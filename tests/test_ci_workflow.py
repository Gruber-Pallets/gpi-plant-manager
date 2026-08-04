from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
    encoding="utf-8"
)


def test_tests_workflow_cancels_superseded_runs():
    expected = """concurrency:
  group: tests-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
"""
    assert expected in TEST_WORKFLOW


def test_tests_workflow_explicitly_opts_into_payroll_guard_test_database():
    assert 'PAYROLL_GUARD_TEST_DATABASE: "1"' in TEST_WORKFLOW
