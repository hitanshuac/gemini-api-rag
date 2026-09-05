from pathlib import Path


def test_rules_directory_exists():
    """Verify that .agents/rules exists and contains files."""
    rules_dir = Path(".agents/rules")
    assert rules_dir.exists(), "Rules directory is missing"
    assert rules_dir.is_dir(), "Rules path is not a directory"
    rule_files = list(rules_dir.glob("*.md"))
    assert len(rule_files) > 0, "No rule files found in .agents/rules"

def test_product_templates_exist():
    """Verify that all 5 product templates exist."""
    templates_dir = Path(".agents/product/templates")
    assert templates_dir.exists(), "Product templates directory is missing"

    expected_templates = [
        "01_PRD.md",
        "02_TAD.md",
        "03_SECURITY.md",
        "04_FRONTEND.md",
        "05_TICKETS.md"
    ]
    for template in expected_templates:
        base_name = template.replace(".md", "")
        has_template = (templates_dir / template).exists() or (templates_dir / f"{base_name}.template").exists()
        assert has_template, f"Missing product template: {template} (or {base_name}.template)"

def test_workflows_directory_exists():
    """Verify that workflows directory exists and contains files."""
    workflows_dir = Path(".agents/workflows")
    assert workflows_dir.exists(), "Workflows directory is missing"
    workflow_files = list(workflows_dir.glob("*.md"))
    assert len(workflow_files) > 0, "No workflow files found in .agents/workflows"
