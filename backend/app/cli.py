"""Команды обслуживания: `python -m app.cli <команда>`. Порядок загрузки данных — DEPLOY.md."""

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path

from app.core.config import settings


def _build_archive(args: argparse.Namespace) -> None:
    from app.archive import build_archive

    report = build_archive(
        raw_dir=args.raw,
        out_dir=args.out,
        years=args.years,
        memory_limit=args.memory,
        threads=args.threads,
        temp_dir=settings.interim_dir / "duckdb",
    )
    print(json.dumps(report["total"], ensure_ascii=False, indent=2))
    print(f"отчёт: {args.out / 'report.json'}, {report['last_build_seconds']} с")


def _load_db(args: argparse.Namespace) -> None:
    from app.loader import Scope, load_db

    report = load_db(
        archive_dir=args.archive,
        database_url=args.database_url,
        work_dir=settings.interim_dir / "load-db",
        scope=Scope(since=args.since, object_ids=args.objects),
        workers=args.workers,
        memory_limit=args.memory,
        threads=args.threads,
        compress_before=None if args.no_compress else args.compress_before,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def _load_external(args: argparse.Namespace) -> None:
    from app.external_loader import ExternalLoadError, load_external

    work = settings.interim_dir / f"load-external-{args.dataset}"
    try:
        report = load_external(
            args.dataset,
            args.prepared,
            database_url=args.database_url,
            work_dir=work,
            workers=args.workers,
            memory_limit=args.memory,
            threads=args.threads,
        )
    except ExternalLoadError as e:
        raise SystemExit(str(e)) from e
    print(
        json.dumps({"rows": report["rows"], "seconds": report["seconds"], "info": report["info"]}, ensure_ascii=False)
    )


def _check_db(args: argparse.Namespace) -> None:
    from app.checks import run_checks
    from app.loader import pg_conninfo

    checks = run_checks(args.archive, pg_conninfo(args.database_url), args.memory)
    for c in checks:
        print(c.line())
    failed = [c for c in checks if not c.ok]
    print(f"проверок: {len(checks)}, расхождений: {len(failed)}")
    if failed:
        raise SystemExit(1)


async def _reverdict(session, args: argparse.Namespace) -> None:
    """Вердикты прошлых тревог прогона — по его основной версии событийной модели (15.2), с записью в журнал."""
    from app.domain.notify import publish
    from app.domain.reverdict import reverdict

    summary, notifications = await reverdict(session, args.run_id)
    if summary["version"] is None:
        await session.rollback()
        print(f"у прогона {args.run_id} нет основной версии событийной модели — пересчитывать нечего")
        return
    print(f"прогон {args.run_id}, основная {summary['version']}: {summary}")
    if not args.yes and input("записать? [y/N] ").strip().lower() != "y":
        await session.rollback()
        print("отменено")
        return
    await session.commit()
    await publish(notifications)
    print("записано, журнал — GET /api/v1/runs/{id}/audit".replace("{id}", str(args.run_id)))


def _runs(args: argparse.Namespace) -> None:
    import asyncio

    from sqlalchemy import select

    from app.db.models import Run
    from app.db.session import SessionLocal
    from app.domain.runs import RunError, delete_run

    async def run() -> None:
        async with SessionLocal() as session:
            if args.action == "list":
                for r in (await session.scalars(select(Run).order_by(Run.id))).all():
                    print(f"{r.id:>5}  {r.status:<9} {r.scenario:<30} {r.sim_start:%Y-%m-%d %H:%M} → {r.sim_now}")
                return
            if args.run_id is None:
                raise SystemExit(f"укажите id прогона: runs {args.action} <id>")
            if args.action == "reverdict":
                await _reverdict(session, args)
                return
            try:
                deleted = await delete_run(session, args.run_id)
            except RunError as e:
                raise SystemExit(str(e)) from e
            if not args.yes:
                answer = input(f"удалить прогон {args.run_id}: {sum(deleted.values())} строк? [y/N] ")
                if answer.strip().lower() != "y":
                    await session.rollback()
                    print("отменено")
                    return
            await session.commit()
            print(f"прогон {args.run_id} удалён: {deleted}")

    asyncio.run(run())


def _reconcile(args: argparse.Namespace) -> None:
    import asyncio

    from sqlalchemy import text

    from app.db.session import SessionLocal
    from app.domain.triage import reconcile_verdicts

    async def run() -> None:
        async with SessionLocal() as session:
            where = "run_id = :run" if args.run is not None else "run_id IS NOT NULL"
            ids = [r[0] for r in await session.execute(text(f"SELECT id FROM alarms WHERE {where}"), {"run": args.run})]
            changed = await reconcile_verdicts(session, ids)
            await session.commit()
            print(f"тревог проверено: {len(ids)}, вердикт исправлен: {len(changed)}")

    asyncio.run(run())


def _close_outcomes(args: argparse.Namespace) -> None:
    import asyncio

    from app.db.session import SessionLocal
    from app.domain.outcomes import close_outcomes, close_outcomes_history
    from app.domain.runs import get_run

    async def run() -> None:
        async with SessionLocal() as session:
            found = await get_run(session, args.run)
            if found is None:
                raise SystemExit(f"прогона {args.run} нет")
            now = found.sim_now or found.sim_start
            if args.history:
                try:
                    counts, _ = await close_outcomes_history(session, found)
                except ValueError as e:
                    raise SystemExit(str(e)) from e
            else:
                counts, _ = await close_outcomes(session, found.id, now)
            await session.commit()
            await session.refresh(found)
            until = found.outcomes_until
            print(
                f"прогон {found.id} ({found.status}), часы {now.isoformat()}: "
                + ", ".join(f"{k} {v}" for k, v in counts.items())
                + (f"; по журналу — до {until.isoformat()}" if args.history and until else "")
            )

    asyncio.run(run())


def _external(args: argparse.Namespace) -> None:
    from app.external.registry import get_dataset

    dataset = get_dataset(args.dataset)
    if args.action == "download":
        from app.external.download import download

        for f in download(dataset, args.external_dir):
            print(f"{f['name']}: {f['status']}, {f['size']} байт, sha256 {f['sha256']}")
    else:
        from app.external.prepare import prepare, prepared_dir

        report = prepare(dataset, args.external_dir, memory_limit=args.memory)
        print(
            json.dumps(
                {key: report[key] for key in ("rows", "events_period", "labels_by_label", "seconds")},
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"отчёт: {prepared_dir(args.external_dir, dataset) / 'report.json'}")


def _access(args: argparse.Namespace) -> None:
    """Вскрытие под охраной: инциденты базовой истории по архиву и сверка с эталоном (access.md §4). Эталон лежит в
    `backend/tests/fixtures/access/`, в образе его нет (`.dockerignore`): без `--check` сверка тогда пропускается,
    с `--check` — ошибка."""
    import asyncio
    import time

    from app.access_history import FIXTURE, SCENARIOS, compare, compute, scenarios, summary, write
    from app.db.session import SessionLocal
    from app.domain.access import rules

    missing = [p for p in (FIXTURE, SCENARIOS) if not p.exists()]
    if args.check and missing:
        raise SystemExit(
            f"--check: нет эталона {', '.join(str(p) for p in missing)} — сверка идёт из рабочей копии backend "
            "(в образе папки tests нет)"
        )
    started = time.perf_counter()
    subs, results = compute(args.archive, rules(), args.memory)
    got = summary(results)
    total = sum(len(res.incidents) for res in results.values())
    print(f"подобъектов с охраной {len(subs)}, записей {total} за {time.perf_counter() - started:.0f} с")
    for year, s in got.items():
        print(f"  {year}: инцидентов {s['incidents']}, снято после открытия {s['dismissed']}, {s['end_reason']}")
    diffs = []
    if FIXTURE.exists():
        diffs = compare(got)
        print(f"эталон {FIXTURE.name}: " + ("совпало" if not diffs else f"расхождений {len(diffs)}"))
        for d in diffs:
            print(f"  {d}")
    else:
        print(f"эталона {FIXTURE.name} нет (в образе папки tests нет) — сверка пропущена")
    if args.check:
        windows = scenarios(args.archive, rules(), memory_limit=args.memory)
        print(f"эталон {SCENARIOS.name}: " + ("совпало" if not windows else f"расхождений {len(windows)}"))
        for d in windows:
            print(f"  {d}")
        raise SystemExit(1 if diffs or windows else 0)

    async def run() -> dict:
        async with SessionLocal() as session:
            written = await write(session, results)
            await session.commit()
            return written

    print(f"записано в базу (run_id = NULL): {asyncio.run(run())}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-archive", help="архив Parquet из data/raw (docs/architecture/data.md §2)")
    p.add_argument("--raw", type=Path, default=settings.raw_dir, help="папка исходников")
    p.add_argument("--out", type=Path, default=settings.archive_dir, help="папка архива")
    p.add_argument("--years", type=int, nargs="*", help="только эти годы; по умолчанию все")
    p.add_argument("--memory", default="8GB", help="лимит памяти DuckDB")
    p.add_argument("--threads", type=int, default=os.cpu_count(), help="потоков DuckDB")
    p.set_defaults(func=_build_archive)

    p = sub.add_parser("load-db", help="рабочая база из архива: базовая история (docs/architecture/data.md §3)")
    p.add_argument("--archive", type=Path, default=settings.archive_dir, help="папка архива")
    p.add_argument("--database-url", default=settings.database_url, help="БД; по умолчанию DATABASE_URL")
    p.add_argument("--since", type=dt.date.fromisoformat, help="события и итоги с этой даты (YYYY-MM-DD, Москва)")
    p.add_argument("--objects", type=int, nargs="*", help="только каналы этих объектов любого уровня")
    p.add_argument("--workers", type=int, default=4, help="параллельных COPY")
    p.add_argument("--memory", default="8GB", help="лимит памяти DuckDB")
    p.add_argument("--threads", type=int, default=os.cpu_count(), help="потоков DuckDB")
    p.add_argument(
        "--compress-before",
        type=dt.date.fromisoformat,
        default=dt.date(2026, 1, 1),
        help="сжать чанки events старше даты; 2026 год остаётся несжатым — в него пишут прогоны",
    )
    p.add_argument("--no-compress", action="store_true", help="не сжимать чанки")
    p.set_defaults(func=_load_db)

    p = sub.add_parser(
        "load-external", help="внешний набор из data/external/<набор>/prepared: справочник, прогон-основа, разметка"
    )
    p.add_argument("--dataset", required=True, help="ключ набора, например magdeburg")
    p.add_argument("--prepared", type=Path, help="папка prepared; по умолчанию data/external/<набор>/prepared")
    p.add_argument("--database-url", default=settings.database_url, help="БД; по умолчанию DATABASE_URL")
    p.add_argument("--workers", type=int, default=4, help="параллельных COPY")
    p.add_argument("--memory", default="4GB", help="лимит памяти DuckDB")
    p.add_argument("--threads", type=int, default=os.cpu_count(), help="потоков DuckDB")
    p.set_defaults(func=_load_external)

    p = sub.add_parser("check-db", help="сверка рабочей базы с архивом и docs/data-format.md (после полной загрузки)")
    p.add_argument("--archive", type=Path, default=settings.archive_dir, help="папка архива")
    p.add_argument("--database-url", default=settings.database_url, help="БД; по умолчанию DATABASE_URL")
    p.add_argument("--memory", default="8GB", help="лимит памяти DuckDB")
    p.set_defaults(func=_check_db)

    p = sub.add_parser(
        "runs",
        help="прогоны: list, delete <id> — удаление только здесь, в API его нет (D22); reverdict <id> — вердикты "
        "прошлых тревог по основной версии событийной модели (15.2)",
    )
    p.add_argument("action", choices=["list", "delete", "reverdict"])
    p.add_argument("run_id", type=int, nargs="?")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p.set_defaults(func=_runs)

    p = sub.add_parser(
        "access", help="вскрытие под охраной: backfill — инциденты базовой истории по архиву (access.md §4)"
    )
    p.add_argument("action", choices=["backfill"])
    p.add_argument("--check", action="store_true", help="только сверить с эталоном, в базу не писать")
    p.add_argument("--archive", type=Path, default=settings.archive_dir, help="папка архива")
    p.add_argument("--memory", default="1GB", help="лимит памяти DuckDB; лишнее — на диск в data/interim")
    p.set_defaults(func=_access)

    p = sub.add_parser("reconcile-verdicts", help="привести вердикты тревог к виду их эпизодов (A6) — после сбоев")
    p.add_argument("--run", type=int, help="только этот прогон; по умолчанию все прогоны")
    p.set_defaults(func=_reconcile)

    p = sub.add_parser(
        "close-outcomes", help="дозакрыть исходы прогнозов прогона по его часам; закрытые исходы не меняются"
    )
    p.add_argument("--run", type=int, required=True, help="прогон")
    p.add_argument(
        "--history",
        action="store_true",
        help="у завершённого прогона — ещё и по журналу после его конца (backend.md §2.3)",
    )
    p.set_defaults(func=_close_outcomes)

    p = sub.add_parser(
        "external",
        help="внешний набор: download — файлы с проверкой sha256, prepare — prepared/ (external-data.md §4)",
    )
    p.add_argument("action", choices=["download", "prepare"])
    p.add_argument("--dataset", required=True, help="ключ набора из config/external_datasets.yaml")
    p.add_argument("--external-dir", type=Path, default=settings.external_dir, help="папка внешних наборов")
    p.add_argument("--memory", default="2GB", help="лимит памяти DuckDB")
    p.set_defaults(func=_external)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
