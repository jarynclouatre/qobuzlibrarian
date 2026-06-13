"""CLI flow for the one-time library-migration tool.

Wires ``library.migrate`` (the planning/copy engine) to the terminal: resolve
and validate the source and destination, build the plan, show a real preview,
confirm, then copy. Local-only — it reorganizes files on disk and never needs a
Qobuz login.
"""
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.library import migrate as engine
from qobuz_librarian.library.scanner import HAVE_MUTAGEN
from qobuz_librarian.ui_cli.colors import C, fmt, format_size, section, truncate
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import confirm


def _prompt_path(msg: str) -> str:
    try:
        return input(fmt(C.CYAN, msg)).strip()
    except EOFError:
        return ""


def _resolve_paths(args):
    """Source and destination from flags, then env, then an interactive prompt.

    Returns (src, dest) as validated Paths, or (None, None) after explaining
    what's wrong."""
    src = (getattr(args, "migrate_src", "") or config.MIGRATE_SRC).strip()
    dest = (getattr(args, "migrate_dest", "") or config.MIGRATE_DEST).strip()
    if not src:
        src = _prompt_path("  Source library to organize: ")
    if not dest:
        dest = _prompt_path("  Destination for the organized copy: ")
    if not src or not dest:
        log.info(fmt(C.RED,
            "  ✗  Need both a source and a destination.\n"
            "     Set QL_MIGRATE_SRC and QL_MIGRATE_DEST, or pass "
            "--migrate-src / --migrate-dest."))
        return None, None

    src, dest = Path(src), Path(dest)
    in_place = bool(getattr(args, "in_place", False))
    err = engine.validate_paths(src, dest, in_place=in_place)
    if err:
        log.info(fmt(C.RED, f"  ✗  {err}"))
        return None, None
    return src, dest


def _progress_printer():
    """Heartbeat progress — a per-file line on a 47k-file library would bury
    everything else, so log on each phase change and every 500 files."""
    state = {"phase": ""}

    def report(phase, current, total, item):
        if phase != state["phase"]:
            state["phase"] = phase
            log.info(fmt(C.GRAY, f"  {phase}…"))
        if total and (current == total or current % 500 == 0):
            log.info(fmt(C.GRAY, f"    {current}/{total}"))

    return report


def _artist_count(entries) -> int:
    return len({e.dest_rel.parts[0] for e in entries if e.dest_rel})


def _print_preview(plan, verbose: bool, in_place: bool) -> None:
    s = plan.summary()
    log.info("")
    log.info(fmt(C.BOLD + C.CYAN, "  Preview"))
    log.info(fmt(C.WHITE,
        f"    {s['place']} file(s) to place across "
        f"{_artist_count(plan.placed)} artist(s)"))
    if s["unplaceable"]:
        log.info(fmt(C.YELLOW,
            f"    {s['unplaceable']} couldn't be identified — left where they are"))
    if s["collision"]:
        log.info(fmt(C.YELLOW,
            f"    {s['collision']} skipped to avoid a name collision"))
    need, free = engine.space_estimate(plan, in_place=in_place)
    if need:
        verb = "move" if in_place else "copy"
        if free is None:
            log.info(fmt(C.GRAY, f"    ≈{format_size(need)} to {verb}"))
        else:
            log.info(fmt(C.GRAY,
                f"    ≈{format_size(need)} to {verb} · "
                f"{format_size(free)} free at the destination"))
            if need > free:
                log.info(fmt(C.BOLD + C.RED,
                    f"    ⚠  Not enough free space: needs about {format_size(need)} "
                    f"but only {format_size(free)} is free. Free up space or pick "
                    "another destination first."))
    if verbose:
        for e in plan.unplaceable[:50]:
            log.info(fmt(C.GRAY, f"      ? {truncate(str(e.source), 70)}"))
        for e in plan.collisions[:50]:
            log.info(fmt(C.GRAY,
                f"      ! {truncate(str(e.source), 55)} — {e.reason}"))


def run_migrate_mode(args):
    section("Library migration — organize an existing collection")

    if not HAVE_MUTAGEN:
        log.info(fmt(C.RED,
            "  ✗  mutagen isn't available, so tags can't be read and every file "
            "would be unidentifiable.\n     Use the bundled image (it includes "
            "mutagen) or `pip install mutagen`."))
        return

    src, dest = _resolve_paths(args)
    if src is None:
        return

    in_place = bool(getattr(args, "in_place", False))
    use_acoustid = bool(getattr(args, "acoustid", False))

    log.info(fmt(C.GRAY, f"  Source:      {src}"))
    log.info(fmt(C.GRAY, f"  Destination: {dest}"))
    if in_place:
        log.info(fmt(C.YELLOW + C.BOLD,
            "  In-place mode: files are MOVED into place — originals are "
            "relocated, not copied, and folders left empty are removed."))
    else:
        log.info(fmt(C.GREEN,
            "  Copy mode: your originals stay exactly where they are."))
    if not use_acoustid:
        log.info(fmt(C.GRAY,
            "  Tags only (fast). Add --acoustid to fingerprint files whose tags "
            "can't place them."))

    progress = _progress_printer()
    items = engine.collect_items(src, use_acoustid=use_acoustid, progress=progress)
    plan = engine.build_plan(items, dest)

    _print_preview(plan, bool(getattr(args, "verbose", False)), in_place)

    # A preview always leaves an auditable artifact, even on a dry run.
    manifest = dest / "migration-manifest.csv"
    try:
        engine.write_manifest(plan, manifest)
        log.info(fmt(C.GRAY, f"  Full plan written to {manifest}"))
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't write the manifest ({e})."))

    if getattr(args, "dry_run", False):
        log.info(fmt(C.CYAN, "  Dry run — nothing was copied."))
        return
    if not plan.placed:
        log.info(fmt(C.GRAY, "  Nothing to place. Stopping."))
        return

    verb = "Move" if in_place else "Copy"
    if not confirm(f"  {verb} {len(plan.placed)} file(s) into {dest}?",
                   default_yes=False, auto_yes=bool(getattr(args, "yes", False))):
        log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
        return

    result = engine.execute_plan(plan, in_place=in_place, progress=progress)

    # Timestamped so a second run (or a resume after a partial run) doesn't
    # overwrite the only source→destination record of the first run's moves —
    # in-place mode already deleted the sources, so that mapping is otherwise
    # unrecoverable.
    from datetime import datetime
    results_manifest = dest / f"migration-results-{datetime.now():%Y%m%d-%H%M%S}.csv"
    try:
        engine.write_results_manifest(result, results_manifest)
        log.info(fmt(C.GRAY, f"  · Results manifest: {results_manifest}"))
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't write the results manifest ({e})."))

    # In-place leaves the emptied source folders behind; clear the husk.
    pruned = engine.prune_empty_dirs(src) if in_place else 0

    log.info("")
    log.info(fmt(C.GREEN,
        f"  ✓  {result.copied} file(s) {'moved' if in_place else 'copied'}."))
    if result.skipped:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {result.skipped} skipped (destination already existed)."))
    if result.lingered:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {result.lingered} moved but the original couldn't be removed "
            f"(still at the source)."))
    if pruned:
        log.info(fmt(C.GRAY,
            f"  Cleared {pruned} now-empty folder(s) from the source."))
    if result.failed:
        log.info(fmt(C.RED, f"  ✗  {result.failed} failed:"))
        for src, reason in result.failures[:50]:
            log.info(fmt(C.RED, f"       {truncate(str(src), 60)} — {reason}"))
        if result.failed > 50:
            log.info(fmt(C.RED,
                f"       … and {result.failed - 50} more — see {results_manifest}"))
    if result.cancelled:
        log.info(fmt(C.YELLOW,
            "  ⚠  Stopped early; the destination holds a partial copy."))
    log.info(fmt(C.GRAY,
        f"  New library: {dest}\n"
        f"  Plan:        {manifest}\n"
        f"  Results:     {results_manifest}\n"
        "  Spot-check it before pointing the tool at it as your main library."))
