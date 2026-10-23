import gzip
import os
import tempfile
import unittest
from unittest.mock import patch

import extract


class FlowSummaryTests(unittest.TestCase):
    def test_stages_and_applied_recipe_families(self):
        with tempfile.TemporaryDirectory() as root:
            run = os.path.join(root, "bucket", "list", "test")
            os.makedirs(run)
            with gzip.open(os.path.join(run, "logbook.log.gz"), "wt") as fh:
                fh.write("| Entering stage: Init\n\tLeaving stage: Init\n"
                         "| Entering stage: Model run\n")
            recipe_dir = os.path.join(root, "ddg_recipe", "inc")
            os.makedirs(recipe_dir)
            recipe = os.path.join(recipe_dir, "recipe.inc")
            with open(recipe, "w") as fh:
                fh.write(";Assembly code for MEMSSMC0_CH0_CR_MC_RDB_CREDITS_0_0_0_MCHBAR:\n"
                         ";Assembly code for MEMSSMC1_CH2_CR_PM_PDWN_config_0_0_0_MCHBAR:\n")
            command = "user.MINIBIOS_BSP_USER_INC_BEFORE_MC_LOCK_PATHS=" + recipe
            with patch.object(extract, "summary", return_value={"cmdline": command}):
                result = extract.flow_summary(run)
            self.assertEqual(result["stages"], [
                {"name": "Init", "completed": True},
                {"name": "Model run", "completed": False},
            ])
            self.assertEqual(result["includes"][0]["registers"],
                             ["MC_RDB_CREDITS", "PM_PDWN_CONFIG"])
            self.assertIsNone(result["includes"][0]["note"])

    def test_recipe_outside_run_parent_is_not_inspected(self):
        with tempfile.TemporaryDirectory() as root:
            run = os.path.join(root, "bucket", "list", "test")
            os.makedirs(run)
            recipe = os.path.join(root, "outside.inc")
            with open(recipe, "w") as fh:
                fh.write(";Assembly code for MC_CR_MC_RDB_CREDITS_0_0_0_MCHBAR:\n")
            command = "user.MINIBIOS_BSP_USER_INC_BEFORE_MC_LOCK_PATHS=" + recipe
            with patch.object(extract, "summary", return_value={"cmdline": command}):
                result = extract.flow_summary(run)
            self.assertEqual(result["includes"][0]["registers"], [])
            self.assertIn("outside", result["includes"][0]["note"])


if __name__ == "__main__":
    unittest.main()
