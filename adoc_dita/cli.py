import argparse
import json
from pathlib import Path
import sys

from .converter import convert_files, convert_text
from .repository import compare, compare_pull_request, refs
from .report import save_report


def parse_attributes(values):
    attributes = {}
    for value in values:
        if not value.strip():
            continue
        key, sep, content = value.partition("=")
        if not sep or not key.strip() or any(c in key for c in " \t\n"):
            raise ValueError("Attributes must be name=value, one per line or -a argument")
        attributes[key] = content
    return attributes


def main(argv=None):
    parser = argparse.ArgumentParser(description="Convert AsciiDoc to validated DITA XML, or compare two Git snapshots.")
    parser.add_argument("--version", action="version", version="adoc-dita 0.2.0")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("convert", help="Convert a file, or - for stdin")
    one.add_argument("file", nargs="?", default="-")
    one.add_argument("-o", "--output", help="Output XML file (default: stdout)")
    one.add_argument("--root", help="Allowed input root for includes (default: input file directory)")
    one.add_argument("--filename", default="document.adoc", help="Stable filename for stdin")
    one.add_argument("--json", action="store_true", help="Return XML plus diagnostics as JSON")
    one.add_argument("--repository", help="Local repository for inherited guide attributes and cross-topic links")
    one.add_argument("--profile", help="Inclusion key from --json when a guide includes the same module more than once")
    diff = sub.add_parser("compare", help="Compare branch tips, tags, or commits")
    diff.add_argument("repository", help="Local Git repository, https://github.com/OWNER/REPO, or https://gitlab.example/GROUP/PROJECT")
    diff.add_argument("--base", required=True)
    diff.add_argument("--target", required=True)
    diff.add_argument("-o", "--output", required=True, help="New or empty output directory")
    diff.add_argument("--include", action="append", help="Topic path pattern; repeatable (default: *.adoc)")
    pull = sub.add_parser("pull-request", help="Convert topics changed or affected by a GitHub pull request or GitLab merge request")
    pull.add_argument("url", help="https://github.com/OWNER/REPO/pull/NUMBER or https://gitlab.example/GROUP/PROJECT/-/merge_requests/NUMBER")
    pull.add_argument("-o", "--output", required=True, help="New or empty output directory")
    pull.add_argument("--include", action="append", help="Topic path pattern; repeatable (default: *.adoc)")
    for command in [one, diff, pull]:
        command.add_argument("--guide", help="Guide entry file relative to the repository; follows its native include and attribute rules")
        command.add_argument("-a", "--attribute", action="append", default=[], help="Attribute name=value; repeatable")
        command.add_argument("--type", choices=["auto", "concept", "task", "reference"], default="auto")
    one.add_argument("--attribute-file", action="append",
                     help="Attributes .adoc file; absolute or relative to --root/input folder")
    diff.add_argument("--attribute-file", action="append",
                      help="Attributes .adoc file. A repository-relative path reads each release's version; one absolute path applies that local file to both releases")
    pull.add_argument("--attribute-file", action="append",
                      help="Attributes .adoc file. A repository-relative path reads the base and PR versions; one absolute path applies that local file to both snapshots")
    listing = sub.add_parser("refs", help="List available branches and tags")
    listing.add_argument("repository")
    server = sub.add_parser("serve", help="Open the local browser interface")
    server.add_argument("--port", type=int, default=8765)
    usage = sub.add_parser("usage", help="Summarize anonymous hosted usage events")
    usage.add_argument("log", help="JSONL file, or captured application stdout")
    usage.add_argument("--json", action="store_true", help="Print the complete summary as JSON")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.port)
            return 0
        if args.command == "refs":
            print("\n".join(refs(args.repository)))
            return 0
        if args.command == "usage":
            from .usage import summarize_usage_file
            summary = summarize_usage_file(args.log)
            if args.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print(f"Uses: {summary['uses']}")
                print(f"Anonymous users: {summary['anonymous_users']}")
                for workflow, values in summary["workflows"].items():
                    outcomes = ", ".join(f"{name}={count}" for name, count in values["outcomes"].items()) or "none"
                    print(f"{workflow}: {values['uses']} uses, {values['anonymous_users']} users ({outcomes})")
            return 0
        attributes = parse_attributes(args.attribute)
        if args.command == "convert":
            if args.attribute_file and len(args.attribute_file) > 1:
                raise ValueError('Content conversion accepts one attributes file; include additional definitions from that file')
            if args.repository:
                from .context import convert_in_repository
                root = Path(args.repository).expanduser().resolve()
                file = Path(args.file).resolve() if args.file != '-' else None
                shared = str(root / args.attribute_file[0]) if args.attribute_file else None
                result = convert_in_repository(file.read_text(encoding='utf-8') if file else sys.stdin.read(),
                                               repository=root, filename=file.name if file else args.filename,
                                               source_path=file.relative_to(root).as_posix() if file else None,
                                               guide=args.guide, profile=args.profile, attributes=attributes,
                                               attribute_file=shared, kind=args.type)
            elif args.guide or args.profile:
                raise ValueError('Use --repository with --guide or --profile for content conversion')
            else:
                file = Path(args.file).resolve() if args.file != '-' else None
                source = file.read_text(encoding='utf-8') if file else sys.stdin.read()
                filename = file.name if file else args.filename
                if args.attribute_file:
                    from .context import convert_standalone
                    base = Path(args.root).expanduser().resolve() if args.root else (file.parent if file else Path.cwd())
                    shared = Path(args.attribute_file[0]).expanduser()
                    shared = shared if shared.is_absolute() else base / shared
                    result = convert_standalone(source, filename=filename, attributes=attributes,
                                                attribute_file=shared, kind=args.type)
                elif file:
                    root = Path(args.root).expanduser().resolve() if args.root else file.parent
                    result = convert_files(root, [file.relative_to(root).as_posix()], attributes=attributes,
                                           attribute_files=args.attribute_file, kind=args.type)[0]
                elif args.attribute_file:
                    raise ValueError('Stdin conversion accepts one attributes file')
                else:
                    result = convert_text(source, filename=filename, attributes=attributes, kind=args.type)
            for diagnostic in result["diagnostics"]:
                print(f'{diagnostic["severity"]}: {diagnostic["message"]}', file=sys.stderr)
            if result['status'] == 'selection':
                print(result['selection_message'], file=sys.stderr)
                for choice in result.get('guide_choices', []):
                    print(f"  --guide {choice['guide']} --profile {choice['key']}  ({choice['context']})", file=sys.stderr)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            elif result.get("xml"):
                if args.output:
                    output = Path(args.output)
                    if args.file != "-" and output.resolve() == Path(args.file).resolve():
                        raise ValueError("Output must not overwrite the AsciiDoc source")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(result["xml"], encoding="utf-8")
                    print(f"Wrote {output}", file=sys.stderr)
                else:
                    print(result["xml"], end="")
            return 2 if result["status"] in ("error", "selection") else 0
        output = Path(args.output)
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("Choose a new or empty output directory")
        repository_attribute_files = []
        uploaded_attribute = None
        for value in args.attribute_file or []:
            path = Path(value).expanduser()
            if not path.is_absolute():
                repository_attribute_files.append(value)
                continue
            if uploaded_attribute:
                raise ValueError('Comparison accepts one absolute attributes file')
            if path.suffix.lower() != '.adoc' or not path.is_file():
                raise ValueError(f'Attributes file not found or not an .adoc file: {path}')
            uploaded_attribute = path
        options = dict(patterns=args.include, attributes=attributes,
                       attribute_files=repository_attribute_files or None,
                       attribute_text=uploaded_attribute.read_text(encoding='utf-8') if uploaded_attribute else '',
                       attribute_filename=uploaded_attribute.name if uploaded_attribute else None,
                       kind=args.type, guide=args.guide,
                       progress=lambda message: print(message, file=sys.stderr))
        if args.command == "pull-request":
            report = compare_pull_request(args.url, **options)
        else:
            report = compare(args.repository, args.base, args.target, **options)
        save_report(report, output)
        print(json.dumps(report["summary"]))
        print(f"Report: {output / 'report.html'}")
        error_count = (report["summary"].get("target_errors", 0)
                       if args.command == "pull-request" else report["summary"]["errors"])
        return 2 if error_count else 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
