import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from visual_direction_advisor import FIELDS, ROLES, inspect_storyboard


def item(role, composition):
    return {"role": role, "visual_direction": {"composition": composition, **{k: "说明" for k in FIELDS}}}


class VisualDirectionAdvisorTests(unittest.TestCase):
    def test_varied_five_roles(self):
        items = [item(r, c) for r, c in zip(ROLES, ["hero_object", "annotated_object", "sequence", "spatial_scene", "decision_path"])]
        result = inspect_storyboard({"items": items})
        self.assertEqual([], result["issues"])
        self.assertTrue(all(not entry["issues"] for entry in result["items"]))
        self.assertIn("Does not render", result["limitations"][0])

    def test_repeated_layout_only_prompts_review(self):
        result = inspect_storyboard({"items": [item(r, "checklist") for r in ROLES]})
        self.assertIn("VISUAL_REPEATED_COMPOSITION_REVIEW", result["issues"])
        self.assertEqual("declared_direction_only", result["authority"])

    def test_historical_missing_direction_remains_unverified(self):
        result = inspect_storyboard({"items": [{"role": r} for r in ROLES]})
        self.assertIn("VISUAL_DIRECTION_NOT_DECLARED", result["items"][0]["issues"])

    def test_wrong_roles_and_malformed_direction(self):
        result = inspect_storyboard({"items": [{"role": "cover", "visual_direction": {"composition": []}}, None]})
        self.assertIn("VISUAL_ROLE_SEQUENCE_REVIEW", result["issues"])
        self.assertIn("VISUAL_COMPOSITION_REVIEW", result["items"][0]["issues"])
        self.assertIn("VISUAL_ITEM_INVALID", result["items"][1]["issues"])

    def test_blank_field_and_invalid_root(self):
        row = item("cover", "hero_object")
        row["visual_direction"]["proof_object"] = " "
        self.assertIn("VISUAL_PROOF_OBJECT_MISSING", inspect_storyboard({"items": [row]})["items"][0]["issues"])
        with self.assertRaises(ValueError):
            inspect_storyboard({"items": None})


if __name__ == "__main__":
    unittest.main()
