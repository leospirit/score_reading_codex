from pathlib import Path

ROOT = Path(r"D:/score_reading_fresh")
REPORT_BUILDER = ROOT / "src/pages/ReportBuilder.tsx"
LEGACY_TEMPLATE = ROOT / "score_reading/src/report/templates/report.html.j2"


def assert_contains(text: str, needle: str, where: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing expected text in {where}: {needle}")


def assert_not_contains(text: str, needle: str, where: str) -> None:
    if needle in text:
        raise AssertionError(f"Unexpected old text still present in {where}: {needle}")


def main() -> None:
    rb = REPORT_BUILDER.read_text(encoding="utf-8")
    legacy = LEGACY_TEMPLATE.read_text(encoding="utf-8")

    assert_contains(rb, "重弱与节奏提示", "ReportBuilder")
    assert_contains(rb, "绿色：自然", "ReportBuilder")
    assert_contains(rb, "红色：需调整", "ReportBuilder")
    assert_contains(rb, "灰色：轻读", "ReportBuilder")
    assert_contains(rb, "大球表示相对更突出，小球表示相对更轻。红色表示该处的重弱处理还可调整。", "ReportBuilder")
    assert_contains(rb, "✅ 节奏自然的一句", "ReportBuilder")
    assert_contains(rb, "🔧 最值得调整的一句", "ReportBuilder")
    assert_not_contains(rb, "Correct stress", "ReportBuilder")
    assert_not_contains(rb, "Incorrect stress", "ReportBuilder")
    assert_not_contains(rb, "Unstressed", "ReportBuilder")
    assert_not_contains(rb, "重读准确率", "ReportBuilder")
    assert_not_contains(rb, "准确率 ", "ReportBuilder")

    assert_contains(legacy, "Stress & Rhythm Guide", "legacy template")
    assert_contains(legacy, "Natural", "legacy template")
    assert_contains(legacy, "Needs Adjustment", "legacy template")
    assert_contains(legacy, "Light", "legacy template")
    assert_contains(legacy, "Key words to highlight a little more:", "legacy template")
    assert_contains(legacy, "Light words to keep gentler:", "legacy template")
    assert_contains(legacy, "This sentence could use a clearer stress contrast", "legacy template")
    assert_contains(legacy, "Practice focus:", "legacy template")
    assert_contains(legacy, "How to Make Key Words Stand Out", "legacy template")
    assert_contains(legacy, "const words = {{ reading_words | tojson | default('[]') }}", "legacy template")
    assert_contains(legacy, "const bestPool = pool.filter((s) => !s.hasMissing);", "legacy template")
    assert_contains(legacy, "const completeCandidates = candidates.filter((s) => s.isCompleteEnough);", "legacy template")
    assert_contains(legacy, "const byPunctuation = Boolean(w.sentence_end) || expectedType === 'strong';", "legacy template")
    assert_not_contains(legacy, "Focus words to strengthen:", "legacy template")
    assert_not_contains(legacy, "Light words to soften:", "legacy template")
    assert_not_contains(legacy, "This sentence needs clearer stress contrast", "legacy template")
    assert_not_contains(legacy, "Missed stress words:", "legacy template")
    assert_not_contains(legacy, "How to Improve Stress", "legacy template")
    assert_not_contains(legacy, "Correct stress", "legacy template")
    assert_not_contains(legacy, "Incorrect stress", "legacy template")
    assert_not_contains(legacy, "Unstressed", "legacy template")
    assert_not_contains(legacy, "Prosody Analysis (Stress & Rhythm)", "legacy template")


if __name__ == "__main__":
    main()

