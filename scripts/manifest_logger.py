#!/usr/bin/env python3
"""manifest_logger.py - validate, log, budget-check and archive a Higgsfield shot manifest.

Subcommands
  validate   Schema + semantic validation (credit gate and anchor-first rules).
  report     Credit burn vs. budget cap, per-row estimate vs. actual.
  approve    Record the user's APPROVE for one row (enforces gates in code).
  log        Record a finished job (job_id, hosted_url, actual credits).
  download   Generate download_assets.sh from hosted_url entries.

Exit codes: 0 ok, 1 validation errors, 2 over budget.
Dependencies: Python 3.9+. Optional: `pip install jsonschema` for full schema validation.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "schemas" / "shot_manifest.schema.json"
KINDS = {"anchor_image", "image", "video", "audio"}
STATUSES = ["planned", "estimated", "approved", "rendering", "completed", "failed"]
GATED = {"approved", "rendering", "completed", "failed"}  # statuses that require user approval
COMMITTED = {"approved", "rendering"}
PENDING = {"planned", "estimated"}
DEFAULT_EXT = {"anchor_image": ".png", "image": ".png", "video": ".mp4", "audio": ".mp3"}
REQUIRED_ENTRY_KEYS = [
    "shot_id", "kind", "status", "model", "aspect_ratio", "prompt",
    "estimated_credits", "actual_credits", "job_id", "hosted_url", "approved",
]


# --------------------------------------------------------------------------- io
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value)}"


# ------------------------------------------------------------------- validation
def structural_errors(manifest: dict, schema_path: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Uses jsonschema when installed, else a basic fallback."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        import jsonschema  # type: ignore

        schema = load_json(schema_path)
        validator = jsonschema.Draft202012Validator(schema)
        for err in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
            path = "/".join(str(p) for p in err.absolute_path) or "(root)"
            errors.append(f"schema: {path}: {err.message}")
        return errors, warnings
    except ImportError:
        warnings.append("jsonschema not installed; ran basic structural checks only (pip install jsonschema).")

    for key in ("manifest_version", "campaign_id", "budget", "entries"):
        if key not in manifest:
            errors.append(f"schema: missing top-level key '{key}'")
    budget = manifest.get("budget", {})
    if not isinstance(budget, dict) or not isinstance(budget.get("cap_credits"), (int, float)) or budget["cap_credits"] <= 0:
        errors.append("schema: budget.cap_credits must be a number > 0")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("schema: entries must be a non-empty array")
        return errors, warnings
    for i, entry in enumerate(entries):
        where = f"entries/{i}"
        for key in REQUIRED_ENTRY_KEYS:
            if key not in entry:
                errors.append(f"schema: {where}: missing '{key}'")
        if entry.get("kind") not in KINDS:
            errors.append(f"schema: {where}: kind must be one of {sorted(KINDS)}")
        if entry.get("status") not in STATUSES:
            errors.append(f"schema: {where}: status must be one of {STATUSES}")
    return errors, warnings


def semantic_errors(manifest: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    entries = manifest.get("entries", [])
    by_id: dict[str, dict] = {}
    for entry in entries:
        sid = entry.get("shot_id")
        if sid in by_id:
            errors.append(f"duplicate shot_id '{sid}'")
        by_id[sid] = entry

    for entry in entries:
        sid = entry.get("shot_id")
        status = entry.get("status")
        kind = entry.get("kind")

        if kind == "video":
            ref = entry.get("anchor_ref")
            anchor = by_id.get(ref) if ref else None
            if not ref:
                errors.append(f"[{sid}] anchor-first: video row has no anchor_ref")
            elif anchor is None:
                errors.append(f"[{sid}] anchor-first: anchor_ref '{ref}' does not exist")
            elif anchor.get("kind") != "anchor_image":
                errors.append(f"[{sid}] anchor-first: '{ref}' is not an anchor_image")
            elif status in GATED and not (anchor.get("status") == "completed" and anchor.get("approved") is True):
                errors.append(
                    f"[{sid}] anchor-first: anchor '{ref}' must be completed and approved before this video is approved or rendered"
                )

        if status in GATED:
            if entry.get("approved") is not True:
                errors.append(f"[{sid}] credit gate: status '{status}' requires approved=true")
            if entry.get("estimated_credits") is None:
                errors.append(f"[{sid}] credit gate: status '{status}' requires estimated_credits")

        if status == "completed":
            for key in ("job_id", "hosted_url", "actual_credits"):
                if entry.get(key) in (None, ""):
                    errors.append(f"[{sid}] completed row is missing '{key}'")
            url = entry.get("hosted_url")
            if url and not str(url).startswith("https://"):
                errors.append(f"[{sid}] hosted_url must be an https URL")

        if status == "failed" and entry.get("actual_credits") is None:
            warnings.append(f"[{sid}] failed job has no actual_credits; verify the balance to confirm nothing was charged")

        est, act = entry.get("estimated_credits"), entry.get("actual_credits")
        if isinstance(est, (int, float)) and isinstance(act, (int, float)) and act > est:
            warnings.append(f"[{sid}] actual credits ({fmt(act)}) exceed the estimate ({fmt(est)})")

    return errors, warnings


def budget_summary(manifest: dict) -> dict:
    budget = manifest.get("budget", {})
    cap = num(budget.get("cap_credits"))
    reserve = num(budget.get("reserve_credits"))
    entries = manifest.get("entries", [])
    spent = sum(num(e.get("actual_credits")) for e in entries if e.get("status") in ("completed", "failed"))
    committed = sum(num(e.get("estimated_credits")) for e in entries if e.get("status") in COMMITTED)
    pending = sum(num(e.get("estimated_credits")) for e in entries if e.get("status") in PENDING)
    effective_cap = cap - reserve
    return {
        "cap": cap,
        "reserve": reserve,
        "effective_cap": effective_cap,
        "spent": spent,
        "committed": committed,
        "pending": pending,
        "approved_total": spent + committed,
        "projected_total": spent + committed + pending,
        "remaining": effective_cap - spent - committed,
        "over_budget": spent + committed > effective_cap + 1e-9,
        "plan_over_budget": spent + committed + pending > effective_cap + 1e-9,
    }


def run_validation(manifest: dict, schema_path: Path) -> tuple[list[str], list[str]]:
    errors, warnings = structural_errors(manifest, schema_path)
    if not errors:  # semantic checks assume a structurally valid manifest
        sem_err, sem_warn = semantic_errors(manifest)
        errors += sem_err
        warnings += sem_warn
        summary = budget_summary(manifest)
        if summary["over_budget"]:
            errors.append(
                f"budget: spent + approved ({fmt(summary['approved_total'])}) exceeds the effective cap ({fmt(summary['effective_cap'])})"
            )
        elif summary["plan_over_budget"]:
            warnings.append(
                f"budget: the full plan ({fmt(summary['projected_total'])}) exceeds the effective cap ({fmt(summary['effective_cap'])}); not all rows can be approved"
            )
    return errors, warnings


# ------------------------------------------------------------------- commands
def cmd_validate(args) -> int:
    manifest = load_json(Path(args.manifest))
    errors, warnings = run_validation(manifest, Path(args.schema))
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        print(f"\n{len(errors)} error(s). Manifest is NOT valid.")
        return 2 if any(e.startswith("budget:") for e in errors) and len(errors) == 1 else 1
    print("OK    manifest is valid.")
    return 0


def cmd_report(args) -> int:
    manifest = load_json(Path(args.manifest))
    errors, warnings = run_validation(manifest, Path(args.schema))
    s = budget_summary(manifest)

    print(f"Campaign: {manifest.get('campaign_id')}")
    print(f"{'shot_id':<22}{'kind':<14}{'model':<22}{'status':<11}{'est':>7}{'actual':>8}")
    print("-" * 84)
    for e in manifest.get("entries", []):
        est = "-" if e.get("estimated_credits") is None else fmt(num(e["estimated_credits"]))
        act = "-" if e.get("actual_credits") is None else fmt(num(e["actual_credits"]))
        print(f"{str(e.get('shot_id')):<22}{str(e.get('kind')):<14}{str(e.get('model')):<22}{str(e.get('status')):<11}{est:>7}{act:>8}")
    print("-" * 84)
    print(f"Budget cap:            {fmt(s['cap'])}   (reserve {fmt(s['reserve'])} -> usable {fmt(s['effective_cap'])})")
    print(f"Spent (actual):        {fmt(s['spent'])}")
    print(f"Approved / in flight:  {fmt(s['committed'])}")
    print(f"Pending (not approved):{fmt(s['pending']):>5}")
    print(f"Projected total:       {fmt(s['projected_total'])}")
    print(f"Remaining after approved spend: {fmt(s['remaining'])}")
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if s["over_budget"]:
        print("STATUS: OVER BUDGET")
        return 2
    if errors:
        print("STATUS: INVALID")
        return 1
    print("STATUS: OK" if not s["plan_over_budget"] else "STATUS: OK (plan exceeds cap; not every row can be approved)")
    return 0


def find_entry(manifest: dict, shot_id: str) -> dict:
    for entry in manifest.get("entries", []):
        if entry.get("shot_id") == shot_id:
            return entry
    raise SystemExit(f"error: no entry with shot_id '{shot_id}'")


def cmd_approve(args) -> int:
    path = Path(args.manifest)
    manifest = load_json(path)
    entry = find_entry(manifest, args.shot_id)
    if entry.get("estimated_credits") is None:
        raise SystemExit(f"error: '{args.shot_id}' has no estimated_credits; run the estimate tool first (credit gate).")
    if entry.get("status") not in ("estimated", "planned", "approved"):
        raise SystemExit(f"error: cannot approve a row in status '{entry.get('status')}'.")
    if entry.get("kind") == "video":
        anchor = find_entry(manifest, entry.get("anchor_ref") or "")
        if not (anchor.get("status") == "completed" and anchor.get("approved") is True):
            raise SystemExit("error: anchor-first rule: the anchor image must be completed and approved first.")
    entry["approved"] = True
    entry["status"] = "approved"
    entry["approved_at"] = now_iso()
    summary = budget_summary(manifest)
    if summary["over_budget"]:
        raise SystemExit(
            f"error: approving '{args.shot_id}' would put approved spend at {fmt(summary['approved_total'])}, above the cap of {fmt(summary['effective_cap'])}. Nothing written."
        )
    save_json(path, manifest)
    print(f"approved {args.shot_id}: est {fmt(num(entry['estimated_credits']))} credits; remaining {fmt(summary['remaining'])}")
    return 0


def cmd_log(args) -> int:
    path = Path(args.manifest)
    manifest = load_json(path)
    entry = find_entry(manifest, args.shot_id)
    if entry.get("approved") is not True:
        raise SystemExit("error: credit gate: row was never approved; refusing to log a render for it.")
    if args.status:
        entry["status"] = args.status
    if args.job_id:
        entry["job_id"] = args.job_id
    if args.hosted_url:
        entry["hosted_url"] = args.hosted_url
    if args.actual_credits is not None:
        entry["actual_credits"] = round(args.actual_credits, 4)
    elif args.balance_before is not None and args.balance_after is not None:
        entry["actual_credits"] = round(args.balance_before - args.balance_after, 4)
    if args.notes:
        entry["notes"] = args.notes
    if entry["status"] in ("completed", "failed"):
        entry["completed_at"] = now_iso()
    errors, warnings = run_validation(manifest, Path(args.schema))
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        raise SystemExit("error: manifest would be invalid after this update. Nothing written.")
    for w in warnings:
        print(f"WARN  {w}")
    save_json(path, manifest)
    print(f"logged {args.shot_id}: status={entry['status']} job_id={entry.get('job_id')} actual={entry.get('actual_credits')}")
    return 0


def guess_ext(url: str, kind: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 6 else DEFAULT_EXT.get(kind, "")


def cmd_download(args) -> int:
    path = Path(args.manifest)
    manifest = load_json(path)
    errors, _ = structural_errors(manifest, Path(args.schema))
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1
    rows = [e for e in manifest["entries"] if e.get("hosted_url") and e.get("status") == "completed"]
    if not rows:
        print("nothing to download: no completed entries with a hosted_url.")
        return 0

    lines = [
        "#!/usr/bin/env bash",
        f"# Generated by manifest_logger.py on {now_iso()} for campaign '{manifest['campaign_id']}'.",
        "# Hosted URLs can expire. Run this soon after rendering and commit the assets you want to keep.",
        "set -euo pipefail",
        f'OUT_DIR="${{1:-{args.outdir}}}"',
        'mkdir -p "$OUT_DIR"',
        "",
        "download() {",
        '  local url="$1" dest="$2"',
        '  if [ -s "$dest" ]; then echo "skip (exists): $dest"; return 0; fi',
        '  if curl -fL --retry 3 --retry-delay 2 -o "$dest" "$url"; then echo "saved: $dest"; else echo "FAILED: $url" >&2; rm -f "$dest"; return 1; fi',
        "}",
        "",
        "status=0",
    ]
    for entry in rows:
        filename = f"{entry['shot_id']}{guess_ext(entry['hosted_url'], entry['kind'])}"
        entry["local_path"] = f"{args.outdir}/{filename}"
        lines.append(f'download {shlex.quote(entry["hosted_url"])} "$OUT_DIR"/{shlex.quote(filename)} || status=1')
    lines += ["", 'if [ "$status" -ne 0 ]; then echo "Some downloads failed." >&2; fi', 'exit "$status"', ""]

    out = Path(args.output)
    out.write_text("\n".join(lines), encoding="utf-8")
    out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if args.write_local_paths:
        save_json(path, manifest)
    print(f"wrote {out} ({len(rows)} asset(s)). Run: bash {out}")
    return 0


# ------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--manifest", default="shot_manifest.json", help="path to shot_manifest.json")
        p.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="path to shot_manifest.schema.json")

    p = sub.add_parser("validate", help="validate schema + credit-gate + anchor-first rules")
    common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("report", help="credit burn vs budget cap")
    common(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("approve", help="record APPROVE for a row")
    common(p)
    p.add_argument("--shot-id", required=True)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("log", help="record a render result")
    common(p)
    p.add_argument("--shot-id", required=True)
    p.add_argument("--status", choices=["rendering", "completed", "failed"])
    p.add_argument("--job-id")
    p.add_argument("--hosted-url")
    p.add_argument("--actual-credits", type=float)
    p.add_argument("--balance-before", type=float, help="balance before the job; actual = before - after")
    p.add_argument("--balance-after", type=float)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("download", help="generate download_assets.sh")
    common(p)
    p.add_argument("--output", default="download_assets.sh")
    p.add_argument("--outdir", default="assets", help="default download directory")
    p.add_argument("--write-local-paths", action="store_true", help="store local_path back into the manifest")
    p.set_defaults(func=cmd_download)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
