"""Mobile presentation policy must not change native or saved model settings."""

import copy
import unittest

from shared.config import NEW_CONVERSATION_MODEL, NEW_CONVERSATION_REASONING
from shared.conversation_catalog import catalog_models
from shared.html_lib import templates


LEGACY_MODELS = ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark")


def model_row(model):
    return {
        "id": model, "model": model, "displayName": model,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
        "serviceTiers": [{"id": "priority", "name": "Fast"}],
    }


class MobileModelCatalogTests(unittest.TestCase):
    def setUp(self):
        catalog_order = [
            "gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-terra",
            "gpt-6-sol", "gpt-5.6-luna", "gpt-6-luna",
        ]
        self.visible = [
            "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", *sorted(LEGACY_MODELS),
        ]
        self.catalog = {"models": [
            model_row(model) for model in catalog_order + sorted(LEGACY_MODELS)
        ]}

    def test_new_picker_exposes_every_public_catalog_model_without_mutation(self):
        before = copy.deepcopy(self.catalog)
        values = catalog_models(self.catalog, {})
        self.assertEqual([m["model"] for m in values], self.visible)
        self.assertEqual(self.catalog, before)
        self.assertEqual(NEW_CONVERSATION_MODEL, "gpt-6-sol")
        self.assertEqual(NEW_CONVERSATION_REASONING, "high")

    def test_each_saved_legacy_model_keeps_full_live_capabilities(self):
        for model in LEGACY_MODELS:
            with self.subTest(model=model):
                settings = {"model": model, "effort": "high", "service_tier": "priority"}
                before = dict(settings)
                values = catalog_models(self.catalog, settings)
                self.assertEqual([m["model"] for m in values], self.visible)
                saved = next(value for value in values if value["model"] == model)
                self.assertIn("high", saved["efforts"])
                self.assertIn("medium", saved["efforts"])
                self.assertEqual(saved["tiers"], [{"id": "priority", "name": "Fast"}])
                self.assertEqual(settings, before)

    def test_outage_keeps_saved_model_effort_and_tier(self):
        settings = {"model": "gpt-5.4-mini", "effort": "high", "service_tier": "priority"}
        for catalog in ({}, None, {"models": []}):
            values = catalog_models(catalog, settings)
            self.assertEqual([m["model"] for m in values], [settings["model"]])
            self.assertEqual(values[0]["efforts"], ["high"])
            self.assertEqual(values[0]["tiers"][0]["id"], "priority")

    def test_astra_efforts_come_from_catalog_not_a_static_allowlist(self):
        values = catalog_models(self.catalog, {"model": "gpt-6-astra"})
        self.assertEqual(values[0]["efforts"], ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.catalog["models"].append(model_row("future-model"))
        self.assertIn("future-model", [m["model"] for m in catalog_models(self.catalog, {})])

    def test_duplicate_and_id_only_rows_keep_every_public_model(self):
        self.catalog["models"].extend([None, {}, model_row("gpt-6-astra")])
        for raw in self.catalog["models"]:
            if isinstance(raw, dict):
                raw.pop("model", None)
        self.assertEqual([m["model"] for m in catalog_models(self.catalog, {})], self.visible)

    def test_project_composer_keeps_sol_selected_and_exposes_all_options(self):
        settings = {"model": NEW_CONVERSATION_MODEL, "effort": NEW_CONVERSATION_REASONING,
                    "permission": "read-only", "service_tier": None}
        body = templates.get_template("project_conversation_composer.html").render(
            project_id="fixture", settings=settings, models=catalog_models(self.catalog, settings),
            catalog_error=False, upload_button_html="", upload_panel_html="",
        )
        for model in self.visible:
            self.assertIn('value="' + model + '"', body)
        from html.parser import HTMLParser
        class Options(HTMLParser):
            selected = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "option" and "selected" in attrs:
                    self.selected.append(attrs.get("value"))
        parser = Options()
        parser.feed(body)
        self.assertIn("gpt-6-sol", parser.selected)
        self.assertIn("high", parser.selected)


if __name__ == "__main__":
    unittest.main()
