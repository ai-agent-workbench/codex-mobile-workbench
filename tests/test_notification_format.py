import unittest

from shared.notification_format import format_completion_notification


class CompletionNotificationFormatTests(unittest.TestCase):
    def notice(self, **changes):
        args = dict(status="completed", conversation_title="优化通知",
                    project_name="工作台", ordinal=3, final_text="不应出现在通知里的回复")
        args.update(changes)
        return format_completion_notification(**args)

    def test_original_four_lines_without_reply_or_extra_headings(self):
        title, body, tag = self.notice()
        self.assertEqual(title, "回复已完成")
        self.assertEqual(tag, "white_check_mark")
        self.assertEqual(body, "项目：工作台  \n任务：优化通知  \n轮次：第 3 轮"
                         "  \n请打开工作台查看详细结果。")
        self.assertNotIn("\n\n", body)
        for text in ("标题：", "正文：", "回复：", "不应出现在通知里的回复"):
            self.assertNotIn(text, body)

    def test_final_reply_never_affects_notification(self):
        expected = self.notice()
        for text in (None, "", "可以。恢复成最早截图里的样式： **✅ 回复已完成**",
                     "# 标题\n\n- 列表\n秘密内容", "很长" * 10000):
            self.assertEqual(self.notice(final_text=text), expected)

    def test_metadata_whitespace_is_collapsed(self):
        _, body, _ = self.notice(conversation_title="  优化\n\n通知\t ",
                                 project_name=" 工作台\n维护 ")
        self.assertEqual(body.split("  \n")[:2], ["项目：工作台 维护", "任务：优化 通知"])
        self.assertEqual(body.count("\n"), 3)

    def test_original_48_character_label_limit(self):
        _, body, _ = self.notice(conversation_title="甲" * 80, project_name="乙" * 80)
        self.assertEqual(body.split("  \n")[0], "项目：" + "乙" * 47 + "…")
        self.assertEqual(body.split("  \n")[1], "任务：" + "甲" * 47 + "…")

    def test_missing_metadata_has_explicit_fallbacks(self):
        for ordinal in (None, "", "invalid", 0):
            _, body, _ = self.notice(project_name=None, conversation_title=" \n",
                                     ordinal=ordinal)
            self.assertEqual(body, "项目：项目对话  \n任务：未命名任务"
                             "  \n轮次：当前轮次  \n请打开工作台查看详细结果。")

    def test_labels_cannot_inject_markdown_structure(self):
        _, body, _ = self.notice(project_name="工作台_[维护]", conversation_title="**任务**")
        self.assertIn("项目：工作台\\_\\[维护\\]", body)
        self.assertIn("任务：\\*\\*任务\\*\\*", body)

    def test_status_and_tags_remain_distinct(self):
        for status, title, tag in [
            ("completed", "回复已完成", "white_check_mark"),
            ("failed", "回复失败", "warning"),
            ("interrupted", "回复已停止", "stop_sign"),
            ("other", "回复已结束", "information_source"),
        ]:
            actual_title, _, actual_tag = self.notice(status=status)
            self.assertEqual((actual_title, actual_tag), (title, tag))

    def test_payload_stays_below_ntfy_limit(self):
        title, body, _ = self.notice(project_name="[🙂]" * 100,
                                     conversation_title="*🙂" * 100, final_text="🙂" * 10000)
        self.assertLess(len((title + "\n" + body).encode("utf-8")), 4096)
