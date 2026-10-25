"""Reports remain self-contained and cannot execute source-provided markup."""

import unittest
from h5reclaim.html_report import render_report_html


class HtmlReportTests(unittest.TestCase):
    def test_report_escapes_paths_and_embedded_json(self):
        malicious = '</script><script>alert("source")</script>'
        report = {"schema_version": 1, "tool": "h5reclaim", "outcome": "partial",
            "source": {"path": malicious}, "dataset": {"path": "/data", "shape": [10]},
            "accepted_elements": 7, "unknown_elements": 3}
        output = render_report_html(report)
        self.assertNotIn(malicious, output)
        self.assertIn("7</b> accepted / 3 unknown elements", output)
        self.assertIn("\\u003c/script", output)
        self.assertNotIn('<script src=', output)


if __name__ == "__main__":
    unittest.main()
