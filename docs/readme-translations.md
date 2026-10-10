# Maintaining the README translations

`README.md` is the English source. The root-level `README.<locale>.md` files provide full editions in Polish, German, French, Spanish, Brazilian Portuguese, Simplified Chinese, Japanese, Korean, Russian, and Arabic. Every README links to all eleven editions. The initial translations follow the 1.0.0rc1 English content at commit `93df650`, with the language menu added afterward.

When the English README changes, update the affected prose in every translation. Preserve commands, paths, link destinations, benchmarks and their limitations, verification boundaries, and distinctions between accepted and unknown values. Review meaning with a fluent speaker, particularly before changing recovery or safety claims. These editions are published by the project; this file does not claim that each one has received native-speaker review.

After updating a translation, replace its `English README SHA-256` HTML comment with the SHA-256 hash of the current UTF-8 `README.md`, then run `python tools/check_readme_translations.py`. The check compares command blocks, inline technical identifiers, links, sections, tables, and source hashes. It cannot judge translation quality or scientific accuracy, so review the changed prose separately. For Arabic, check right-to-left readability while leaving code examples and filenames intact.
