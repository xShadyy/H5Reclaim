"""Small Python entry point for the same recovery workflow as the CLI."""

from pathlib import Path
from dataclasses import asdict

from .large_streaming import LargeBudget


def rescue(source, *, dataset=None, output=None, report=None, budget=None,
           resume_dir=None, html_report=None):
    """Recover a selected dataset or the whole file and return its report.

    Destinations must be new. A LargeBudget or a dict can configure quotas.
    Missing measurements remain nonaccepted in the returned validity maps.
    """
    source = Path(source)
    output = Path(output) if output is not None else source.with_name(source.stem + ".recovered.h5")
    report = Path(report) if report is not None else output.with_name(output.stem + ".report.json")
    options = asdict(budget) if isinstance(budget, LargeBudget) else budget
    LargeBudget(**(options or {}))
    if html_report is not None and (Path(html_report).exists() or Path(html_report).is_symlink()):
        raise FileExistsError("HTML destination already exists")
    if dataset is None:
        from .whole_file import rescue_all
        result = rescue_all(source, output, report, streaming_budget=options, resume_dir=resume_dir)
    else:
        from .rescue import auto_rescue
        result = auto_rescue(source, dataset, output, report, streaming_budget=options, resume_dir=resume_dir)
    if html_report is not None:
        from .html_report import write_html_report
        write_html_report(report, html_report)
    return result
