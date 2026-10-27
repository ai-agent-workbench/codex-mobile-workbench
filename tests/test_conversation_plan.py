import copy
import hashlib
import unittest

from engine.conversation_plan import (
    approved_context, matching_plan, plan_confirmation_candidate,
)


def command(command_id, *, text, state="completed", origin="user", kind="steer", attachments=None):
    payload = {}
    if attachments is not None:
        payload["attachments"] = attachments
    return {"id": command_id, "kind": kind, "origin": origin, "state": state,
            "text": text, "payload": payload}


def snapshot(*, later=None, plan_status="completed", plan_text="实施 A"):
    plan_turn = {
        "id": "plan-turn", "ordinal": 1, "status": "completed",
        "commands": [command("ask-plan", text="请给计划")],
        "items": [{"kind": "plan", "native_item_id": "plan-item", "status": plan_status,
                   "text": plan_text, "is_current_source": True}],
    }
    turns = [plan_turn] + (later or [])
    all_commands = [*plan_turn["commands"]]
    for turn in turns[1:]:
        all_commands.extend(turn.get("commands", []))
    return {"conversation": {"id": "conversation", "thread_id": "thread"},
            "turns": turns, "commands": all_commands, "queue": []}


def binding(value):
    return {key: value["binding"][key] for key in ("turn_id", "item_id", "plan_digest")}


class PlanConfirmationCandidateTests(unittest.TestCase):
    def test_direct_keeps_legacy_binding_and_goal_turn_does_not_change_it(self):
        value = snapshot(later=[{
            "id": "goal", "ordinal": 2, "status": "inProgress", "items": [],
            "commands": [command("goal", text="", origin="system", kind="goal_continue")],
        }])
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("direct", candidate["state"])
        self.assertIsNotNone(matching_plan(value, binding(candidate)))

    def test_completed_user_supplements_require_review_and_are_ordered(self):
        attachments = [{"id": "a" * 36, "name": "evidence.txt", "size": 3,
                        "mime_type": "text/plain", "preview_kind": "text",
                        "created_at": "2026-01-01T00:00:00Z", "sha256": "b" * 64,
                        "path": "/private/frozen/evidence.txt", "identity": [1, 2, 3, 4]}]
        value = snapshot(later=[
            {"id": "ordinary-1", "ordinal": 2, "status": "completed", "items": [], "commands": [
                command("steer-1", text="先保留 X", attachments=attachments),
                command("steer-2", text="再加 Y"),
            ]},
            {"id": "ordinary-2", "ordinal": 3, "status": "completed", "items": [], "commands": [
                command("steer-3", text="最后 Z"),
            ]},
        ])
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("review", candidate["state"])
        self.assertEqual(["steer-1", "steer-2", "steer-3"], [x["id"] for x in candidate["supplements"]])
        self.assertNotEqual("/private/frozen/evidence.txt", candidate["context_digest"])
        legacy = binding(candidate)
        self.assertIsNone(matching_plan(value, legacy))
        reviewed = {**legacy, "context_digest": candidate["context_digest"]}
        self.assertIsNotNone(matching_plan(value, reviewed))
        changed_reference = copy.deepcopy(value)
        changed_reference["turns"][1]["commands"][0]["payload"]["attachments"][0]["path"] = "/private/other/evidence.txt"
        self.assertIsNone(matching_plan(changed_reference, reviewed), "固定附件引用变化也需重新核对")

    def test_review_digest_changes_for_supplement_but_not_goal_turn(self):
        later = [{"id": "ordinary", "ordinal": 2, "status": "completed", "items": [],
                  "commands": [command("steer", text="补充")]}]
        value = snapshot(later=later)
        first = plan_confirmation_candidate(value)
        with_goal = copy.deepcopy(value)
        with_goal["turns"].append({"id": "goal", "ordinal": 3, "status": "inProgress", "items": [],
            "commands": [command("goal", text="", origin="system", kind="goal_continue")]})
        self.assertEqual(first["context_digest"], plan_confirmation_candidate(with_goal)["context_digest"])
        changed = copy.deepcopy(value)
        changed["turns"][1]["commands"][0]["text"] = "修改过的补充"
        self.assertIsNone(matching_plan(changed, {**binding(first), "context_digest": first["context_digest"]}))

    def test_unattributed_active_and_nonuser_turns_block_old_plan(self):
        cases = [
            {"id": "unknown", "ordinal": 2, "status": "inProgress", "items": [], "commands": []},
            {"id": "active", "ordinal": 2, "status": "inProgress", "items": [],
             "commands": [command("active", text="等待")]},
            {"id": "scheduled", "ordinal": 2, "status": "completed", "items": [],
             "commands": [command("scheduled", text="任务", origin="scheduled")]},
        ]
        for turn in cases:
            with self.subTest(turn=turn["id"]):
                candidate = plan_confirmation_candidate(snapshot(later=[turn]))
                self.assertEqual("blocked", candidate["state"])
                self.assertIsNone(matching_plan(snapshot(later=[turn]), binding(candidate)))

    def test_associated_delivery_unknown_is_not_promoted_to_review(self):
        value = snapshot(later=[{
            "id": "ordinary", "ordinal": 2, "status": "completed", "items": [],
            "commands": [command("unknown", text="可能送达", state="delivery_unknown")],
        }])
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("blocked", candidate["state"])
        self.assertEqual("ordinary_input_pending", candidate["reason"])

    def test_steer_within_planning_turn_is_also_reviewed(self):
        value = snapshot()
        supplement = command("same-turn", text="还有这项要求")
        value["turns"][0]["commands"].append(supplement)
        value["commands"].append(supplement)
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("review", candidate["state"])
        self.assertEqual(["ask-plan", "same-turn"], [item["id"] for item in candidate["supplements"]])
        self.assertIsNone(matching_plan(value, binding(candidate)))
        self.assertIsNotNone(matching_plan(value, {**binding(candidate), "context_digest": candidate["context_digest"]}))

    def test_pending_receipt_in_plan_turn_is_not_hidden_by_turn_association(self):
        value = snapshot()
        pending = command("same-turn-unknown", text="未知投递", state="delivery_unknown")
        value["turns"][0]["commands"].append(pending)
        value["commands"].append(pending)
        self.assertEqual("blocked", plan_confirmation_candidate(value)["state"])

    def test_plan_turn_queued_user_input_before_auto_marker_is_not_omitted(self):
        value = snapshot()
        user = command("queued-before-auto", text="必须保留这项补充", attachments=[{"id": "a", "name": "补充.txt"}])
        auto = command("auto", text="", kind="goal_continue", origin="system")
        value["turns"][0]["commands"] = [user, auto]
        value["commands"] = [user, auto]
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("review", candidate["state"])
        self.assertEqual(["queued-before-auto"], [item["id"] for item in candidate["supplements"]])
        self.assertEqual("plan_turn", candidate["supplements"][0]["scope"])
        self.assertIsNone(matching_plan(value, binding(candidate)))

    def test_completed_goal_turn_with_user_steer_is_reviewable(self):
        value = snapshot(later=[{"id": "mixed", "ordinal": 2, "status": "completed", "items": [], "commands": [
            command("auto", text="", kind="goal_continue", origin="system"),
            command("user-steer", text="新的实际要求"),
        ]}])
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("review", candidate["state"])
        self.assertEqual(["user-steer"], [item["id"] for item in candidate["supplements"]])
        self.assertIsNotNone(matching_plan(value, {**binding(candidate), "context_digest": candidate["context_digest"]}))

    def test_definitely_rejected_stop_does_not_invalidate_plan(self):
        value = snapshot()
        rejected = {**command("stop", text="", kind="interrupt", state="rejected"), "reason": "stale_turn"}
        value["turns"][0]["commands"].append(rejected)
        value["commands"].append(rejected)
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("direct", candidate["state"])
        self.assertIsNotNone(matching_plan(value, binding(candidate)))
        rejected["state"] = "delivery_unknown"
        self.assertEqual("blocked", plan_confirmation_candidate(value)["state"])

    def test_pending_unknown_input_blocks_even_without_queue(self):
        value = snapshot()
        value["commands"].append(command("unknown", text="已发但未确认", state="delivery_unknown"))
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("blocked", candidate["state"])
        self.assertEqual("ordinary_input_pending", candidate["reason"])

    def test_newer_incomplete_plan_blocks_prior_completed_plan(self):
        value = snapshot(later=[{
            "id": "new-plan", "ordinal": 2, "status": "inProgress",
            "commands": [command("new-plan-message", text="修订")],
            "items": [{"kind": "plan", "native_item_id": "new-plan", "status": "inProgress",
                       "text": "新计划", "is_current_source": True}],
        }])
        candidate = plan_confirmation_candidate(value)
        self.assertEqual("blocked", candidate["state"])
        self.assertEqual("latest_plan_not_completed", candidate["reason"])
        old = {"turn_id": "plan-turn", "item_id": "plan-item",
               "plan_digest": hashlib.sha256("实施 A".encode()).hexdigest()}
        self.assertIsNone(matching_plan(value, old))

    def test_review_context_explicitly_preserves_supplements(self):
        value = snapshot(later=[{"id": "later", "ordinal": 2, "status": "completed", "items": [],
                                  "commands": [command("later-command", text="必须额外验证")]}])
        candidate = plan_confirmation_candidate(value)
        result = approved_context(candidate["plan"], {**binding(candidate), "context_digest": candidate["context_digest"]}, candidate["supplements"])
        self.assertIn("后续用户补充要求仍有效", result)
        self.assertIn("必须额外验证", result)


if __name__ == "__main__":
    unittest.main()
