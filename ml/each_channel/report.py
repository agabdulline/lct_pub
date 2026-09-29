"""Render the frozen per-type results without changing models or thresholds."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def pct(x):
    return f"{x:.1%}" if x is not None else "—"


def num(x):
    return f"{x:.2f}" if x is not None else "—"


def results_table(results):
    rows = [
        "| Тип | Обучение: строк / + | Тест: строк / + | Тревог | P | R | F1 | AP | Медиана дней | Пороги |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in results:
        if item["status"] != "trained":
            tr, te = item["counts"]["train"], item["counts"]["test"]
            rows.append(
                f"| {item['name']} | {tr['decisions']:,} / {tr['positives']:,} (не обучена) | "
                f"{te['decisions']:,} / {te['positives']:,} | — | — | — | — | — | — | не оценивается |"
            )
            continue
        s, t = item["selected"], item["test"]
        rows.append(
            f"| {item['name']} | {s['refit_rows']:,} / {s['refit_positives']:,} | "
            f"{t['decisions']:,} / {t['positives']:,} | {t['alerts']:,} | "
            f"{pct(t['precision'])} | {pct(t['recall'])} | {num(t['f1'])} | {num(t['ap'])} | "
            f"{num(t['lead_days']['0.5'])} | {'да' if t['passes_both'] else ('нет' if t['positives'] else 'не оценивается')} |"
        )
    return "\n".join(rows)


def render(folder: Path):
    results = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((folder / "models").glob("*/config.json"))
    ]
    trained = [r for r in results if r["status"] == "trained"]
    passed = sum(r["test"]["passes_both"] for r in trained)
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    lines = [
        "# each_channel: отдельные ежедневные модели",
        "",
        f"Обучено {len(trained)} моделей. На тесте одновременно Precision > 0.7 и Recall > 0.5: **{passed}**.",
        "",
        "## Задача и состав",
        "",
        "Выбраны типы, чья доля строго больше 5% от 11 485 уникальных каналов справочника.",
        "Одна модель обслуживает все каналы своего типа; прогноз индивидуален для пары «канал — сутки».",
        "Цель: наблюдаемое начало записи `Неисправен` в [T+1 сутки, T+30 суток). Физический износ",
        "и точная дата отказа не размечены. Каналы уже в неисправности или без событий за 30 суток не оцениваются.",
        "Цель означает наличие эпизода в указанном окне: эпизод в первые 24 часа не отменяет метку",
        "по более позднему эпизоду. Это соответствует коду v0, но расходится с вводным описанием daily.py",
        "про ближайший отказ; расхождение зафиксировано в docs/QUESTIONS.md.",
        "",
        "| Тип | Каналов | Доля каталога |",
        "|---|---:|---:|",
    ]
    for info in protocol["types"].values():
        lines.append(f"| {info['name']} | {info['channels']:,} | {pct(info['share'])} |")
    verification_path = folder / "verification.json"
    verification = (
        json.loads(verification_path.read_text(encoding="utf-8"))
        if verification_path.exists()
        else {}
    )
    if "source_distribution" in verification:
        names = {v["name"] for v in protocol["types"].values()}
        lines += [
            "",
            "События подготовленного источника (история и будущее для разметки; не число обучающих строк):",
            "",
            "| Тип | Источник / годы | Каналов с событиями | Суточных сводок | Событий | Сообщений `Неисправен` |",
            "|---|---|---:|---:|---:|---:|",
        ]
        restored_types = verification["data_manifest"]["restored_types"]
        for row in verification["source_distribution"]:
            if row["name"] in names:
                period = "архив 2021–2026" if row["name"] in restored_types else "v3, 2019–2026"
                lines.append(
                    f"| {row['name']} | {period} | {row['active_channels']:,} | {row['daily_rows']:,} | "
                    f"{row['events']:,} | {row['fault_messages']:,} |"
                )
    lines += [
        "",
        "## Данные",
        "",
        "Для восьми типов сохранены исходные подготовленные sensor_daily/fault_episodes из sensor_failure_v3.",
        "Этот исходный бандл содержит историю с 2019 года по 30 июня 2026; обучение начинается с 2022.",
        "Для остальных 11 типов восстановлены сводки и эпизоды из локального `dataset.zip`:",
        "журналы 2021–2026, включая фазу, переключатели и УИР-Р. Источник не изменялся.",
        "2021 используется для истории; даты до 2022 не являются обучающими решениями.",
        "Восстановленные события сортируются по времени и ID, точные дубли внутри годовых файлов убраны;",
        "повторный заголовок 2025 пропущен. Правила подготовки исходного v3-бандла здесь не переопределяются.",
        "Промежуточные данные и прогнозы находятся в data/interim/each_channel, вне Git.",
        "Справочник текущий: историческое соответствие каналов физическим приборам не доказано.",
        "Отрицательная метка означает отсутствие целевого журнального начала в наблюдаемом окне,",
        "а не подтверждённую физическую исправность. Пропущенные журналом отказы оценить невозможно.",
        "",
        "## Честная временная проверка",
        "",
        "| Период решений | Назначение |",
        "|---|---|",
        "| 2022–2024, понедельники | Начальное обучение; у recent-вариантов только 2024 |",
        "| I полугодие 2025, каждый день | Ранняя остановка по невзвешенной PR AUC |",
        "| Обучение + I полугодие 2025 | Переобучение с выбранным числом деревьев: сначала понедельники, затем отдельный вариант со всеми днями полугодия |",
        "| III квартал 2025, каждый день | Выбор варианта отдельно для каждого типа: 7 исходных + 6 с новыми признаками + 1 с ежедневным дообучением для пяти оцениваемых типов |",
        "| IV квартал 2025, каждый день | Независимый от обучения подбор рабочего порога победителя |",
        "| 1 января — 2 мая 2026, каждый день | Итоговая проверка зафиксированных модели и порога |",
        "",
        "В каждом периоде исключены решения, для которых T+30 суток выходит за его правую границу.",
        "Из целей исключены окна с пропусками 7–8 апреля 2024 и 1 июня 2026; конец бандла — 30 июня 2026.",
        "У модели выбран максимум min(P/0.7,R/0.5) на III квартале; финальный порог пересчитан только на IV.",
        "Минимум 10 предупреждений при подборе порога. При отсутствии предупреждений Precision не определён.",
        "selection_frozen.json записан до тестового скоринга. 2026 уже изучался командой ранее:",
        "это отложенный период текущего запуска, но не совершенно новый слепой тест.",
        "",
        "## Признаки и варианты",
        "",
        "Базовые 29 признаков v0: активность, тревоги, числовые показания, история неисправностей и календарь.",
        "Соседи считаются по всем доступным типам объекта, а не только по целевому типу.",
        "Расширение: неизвестные/отключённые/нормальные состояния, дым/движение/открытие за 1/7/30/90 дней;",
        "доли состояний, изменение активности и показаний, числовой разброс, медиана интервалов между",
        "прошлыми неисправностями, история объекта и группы тега. Все окна строго до T.",
        "Второй этап добавляет последнее состояние и показание, давность событий и число активных дней,",
        "частоты положительных 30-дневных исходов за 180/365 дней. Используются только полностью",
        "завершённые горизонты: исторические решения U не позднее T−30. Для соседей собственный канал исключён.",
        "Отдельно проверены категории объекта/группы и идентификатор канала. Последний может помочь",
        "на знакомых каналах, но не доказывает переносимость на новые физические приборы.",
        "Флаг `last_numeric_error` — эвристика для значений −127 и ≤−3000; физический смысл этих кодов не подтверждён.",
        "Проверены глубины 4/5/6/8, L2 3/5/10/20, SqrtBalanced, обучение с 2024 года.",
        "Это ограниченный воспроизводимый поиск; глобальная оптимальность гиперпараметров не утверждается.",
        "Третий этап меняет только частоту примеров при дообучении на I полугодии 2025; число деревьев",
        "и признаки зафиксированы по победителю второго этапа. Победитель выбирается тем же правилом на III квартале.",
        "",
        "## Итог на 2026",
        "",
        results_table(results),
        "",
        "P — доля верных предупреждений; R — доля найденных положительных канал-дней; AP — average precision.",
        "Медиана дней относится к верным предупреждениям. Один эпизод может давать положительные строки",
        "для нескольких предыдущих суток. Их нельзя считать независимыми физическими отказами.",
        "",
        "![Precision и Recall](figures/quality.png)",
        "",
        "## Выбранные модели и перенос порога",
        "",
        "| Тип | Вариант | Признаков | Деревьев | Порог | III кв.: P/R | IV кв.: P/R |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in trained:
        s, c = r["selected"], r["calibration"]
        lines.append(
            f"| {r['name']} | {s['recipe']['name']} | {len(s['features'])} | {s['trees']} | "
            f"{r['threshold']:.6f} | {pct(s['selection']['precision'])}/{pct(s['selection']['recall'])} | "
            f"{pct(c['precision'])}/{pct(c['recall'])} |"
        )
    archive = folder / "stage1/models"
    if archive.exists():
        lines += [
            "",
            "## Что дал дополнительный этап",
            "",
            "Победитель заменён только при улучшении критерия на III квартале 2025. Тест не участвовал",
            "в выборе. Поэтому отдельные тестовые показатели после замены могут снизиться.",
            "",
            "| Тип | Этап 1: P / R / AP | Итог: P / R / AP | Модель заменена |",
            "|---|---:|---:|---|",
        ]
        for r in trained:
            old = json.loads((archive / r["key"] / "config.json").read_text(encoding="utf-8"))
            a, b = old["test"], r["test"]
            changed = old["selected"] != r["selected"]
            lines.append(
                f"| {r['name']} | {pct(a['precision'])} / {pct(a['recall'])} / {num(a['ap'])} | "
                f"{pct(b['precision'])} / {pct(b['recall'])} / {num(b['ap'])} | {'да' if changed else 'нет'} |"
            )
    lines += [
        "",
        "## Где оценка невозможна или ненадёжна",
        "",
        "Переключатель: во всех пяти периодах нет положительных меток; бинарная модель не обучена.",
        "УИР-Р: модель обучена, но на подборе порога и тесте нет положительных примеров; Recall",
        "и прохождение требований не оцениваются. Отсутствие обнаруженных отказов не доказывает безотказность.",
        "Для движения, температуры и дверей число каналов с отказами мало; повторяющиеся канал-дни",
        "не увеличивают число независимых приборов. Даже высокий Recall по эпизодам нельзя подменять",
        "им требуемый Recall по решениям. Ни один результат не является оценкой физического износа.",
        "",
        "| Тип | Каналов в тесте | Каналов с целевым отказом | Каналов с верным предупреждением |",
        "|---|---:|---:|---:|",
    ]
    for r in trained:
        t = r["test"]
        lines.append(
            f"| {r['name']} | {t['channels']} | {t['positive_channels']} | {t['caught_channels']} |"
        )
    lines += [
        "",
        "## История неисправностей канала",
        "",
        "История означает наблюдаемое начало неисправности в 2022 — I полугодии 2025.",
        "",
        "| Тип | История | Решений / + | Тревог | P | R | Медиана дней |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in trained:
        for key, label in (("with_history", "есть"), ("without_history", "нет")):
            m = r["test_by_fault_history"][key]
            lines.append(
                f"| {r['name']} | {label} | {m['decisions']:,}/{m['positives']:,} | {m['alerts']:,} | "
                f"{pct(m['precision'])} | {pct(m['recall'])} | {num(m['lead_days']['0.5'])} |"
            )
    lines += [
        "",
        "## Объекты, встречавшиеся при обучении",
        "",
        "Новый объект — объект без допустимых обучающих решений до июля 2025. Если таких строк",
        "нет, эта проверка не даёт свидетельства обобщения на новые объекты.",
        "",
        "| Тип | Объект | Решений / + | P | R |",
        "|---|---|---:|---:|---:|",
    ]
    for r in trained:
        for key, label in (("known_object", "знакомый"), ("new_object", "новый")):
            m = r["test_by_object_history"][key]
            lines.append(
                f"| {r['name']} | {label} | {m['decisions']:,}/{m['positives']:,} | "
                f"{pct(m['precision'])} | {pct(m['recall'])} |"
            )
    lines += [
        "",
        "## Эпизоды и простые правила",
        "",
        "Эпизод здесь — уникальная пара channel_id/target_start, представленная хотя бы одной положительной",
        "строкой теста. Это отдельная метрика, не полнота по всем отказам оборудования.",
        "",
        "| Тип | Найдено / представлено эпизодов | Медиана первого предупреждения, дней | Всегда тревога: P/R | Недавний отказ: P/R |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in trained:
        t, a, b = (
            r["test"],
            r["baselines"]["always_alert"],
            r["baselines"]["recent_fault"],
        )
        lines.append(
            f"| {r['name']} | {t['caught_represented_episodes']}/{t['represented_episodes']} | "
            f"{num(t['episode_first_alert_median_days'])} | {pct(a['precision'])}/{pct(a['recall'])} | "
            f"{pct(b['precision'])}/{pct(b['recall'])} |"
        )
    lines += [
        "",
        "## Полный журнал выбора",
        "",
        "Все показатели следующей таблицы относятся к III кварталу 2025, а не к тесту.",
        "",
        "| Тип | Вариант | Деревьев | P | R | AP |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in trained:
        for c in r["candidates"]:
            m = c["selection"]
            lines.append(
                f"| {r['name']} | {c['recipe']['name']} | {c['trees']} | {pct(m['precision'])} | "
                f"{pct(m['recall'])} | {num(m['ap'])} |"
            )
    lines += [
        "",
        "## Важные признаки",
        "",
        "Важность CatBoost описывает использование признака моделью, а не причинность отказа.",
        "",
        "| Тип | Пять наиболее важных признаков (вес) |",
        "|---|---|",
    ]
    for r in trained:
        top = ", ".join(
            f"`{name}` ({weight:.1f})" for name, weight in list(r["feature_importance"].items())[:5]
        )
        lines.append(f"| {r['name']} | {top} |")
    lines += [
        "",
        "## Устойчивость по месяцам теста",
        "",
        "Май представлен только 1–2 числами. Порог во всех месяцах один и выбран на IV квартале 2025.",
        "",
        "| Тип | Месяц | Решений / + | Тревог | P | R | Медиана дней |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in trained:
        for month, m in r["test_by_month"].items():
            lines.append(
                f"| {r['name']} | {month} | {m['decisions']:,}/{m['positives']:,} | {m['alerts']:,} | "
                f"{pct(m['precision'])} | {pct(m['recall'])} | {num(m['lead_days']['0.5'])} |"
            )
    timings = verification.get("models", {})
    if timings and all(
        "build_and_score_seconds" in r for m in timings.values() for r in m["parity"]
    ):
        lines += [
            "",
            "## Локальное время расчёта",
            "",
            "Замер построения признаков и скоринга по срезу реальной истории, три даты на модель.",
            "Загрузка исходного набора измерена отдельно. Это локальный офлайн-замер, не проверка API или SLA.",
            "",
            "| Тип | Минимум, с | Максимум, с |",
            "|---|---:|---:|",
        ]
        totals = {}
        names = {r["key"]: r["name"] for r in trained}
        for key, item in timings.items():
            values = [r["build_and_score_seconds"] for r in item["parity"]]
            lines.append(f"| {names[key]} | {min(values):.2f} | {max(values):.2f} |")
            for row in item["parity"]:
                totals[row["date"]] = totals.get(row["date"], 0) + row["build_and_score_seconds"]
        load_seconds = verification.get("source_load_seconds", 0)
        maximum = max(totals.values()) + load_seconds
        lines += [
            "",
            f"Загрузка источника: {load_seconds:.2f} с. Максимальная сумма шести последовательных расчётов",
            f"на одну дату плюс одна загрузка: **{maximum:.2f} с**. "
            + (
                "Это укладывается в 300 с на данной машине."
                if maximum < 300
                else "Это больше 300 с: требование скорости для последовательного полного расчёта не подтверждено."
            ),
            "В сумму не включена загрузка самих моделей с диска. Повторное построение общего контекста",
            "для разных типов в этом CLI не кэшируется; производственный запуск требует отдельного замера.",
        ]
    lines += [
        "",
        "## Проверки и воспроизведение",
        "",
        "Тесты: исключение будущих событий и событий ровно в T; совпадение признаков полной истории и",
        "среза до T; граница 30 суток между периодами; строгие пороги P/R и неопределённый P без тревог.",
        "Дополнительные результаты проверок и контрольные суммы — verification.json.",
        "Зависимости закреплены в pyproject.toml/uv.lock. Команды выполняются из корня worktree;",
        "для каждой команды ниже можно заменить `python` на `uv run --project ml/each_channel --frozen python`.",
        "",
        "```powershell",
        "python -m ml.each_channel.prepare --source <v3-data> --archive <dataset.zip> --out data/interim/each_channel/source",
        "python -m ml.each_channel.train --data-dir data/interim/each_channel/source --resume",
        "python -m ml.each_channel.improve",
        "python -m ml.each_channel.improve_dense",
        "python -m ml.each_channel.verify",
        "python -m ml.each_channel.report",
        "python -m pytest ml/each_channel/test_pipeline.py -q",
        "python -m ml.each_channel.predict --data-dir data/interim/each_channel/source --model-dir ml/each_channel/models/smoke --date 2026-03-01 --out data/interim/each_channel/prediction.csv",
        "```",
        "",
        "Сервис не переключён на экспериментальные модели. В прогнозах нет собственного предсказанного",
        "состояния в истории; используются только реальные события. Код обучения и CLI скоринга общий.",
        "Веса `.cbm` сохранены локально рядом с config.json и исключены из Git; stage1 хранит первый этап.",
        "Медиана упреждения — статистика верных предупреждений, а не предсказанная дата или оценка ошибки даты.",
        "",
        "## Источники методики",
        "",
        "- [CatBoost: глубина, регуляризация и ранняя остановка](https://catboost.ai/docs/en/concepts/parameter-tuning)",
        "- [CatBoost: веса классов и временной порядок](https://catboost.ai/docs/en/references/training-parameters/common)",
        "- [scikit-learn: отдельные данные для настройки порога](https://scikit-learn.org/stable/modules/classification_threshold.html)",
        "",
    ]
    appendix = folder / "REPORT_APPENDIX.md"
    if appendix.exists():
        lines.extend(["", appendix.read_text(encoding="utf-8").rstrip(), ""])
    (folder / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    if trained:
        (folder / "figures").mkdir(exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 5))
        positions = np.arange(len(trained))
        ax.bar(
            positions - 0.18,
            [r["test"]["precision"] or 0 for r in trained],
            width=0.36,
            label="Precision",
        )
        ax.bar(
            positions + 0.18,
            [r["test"]["recall"] or 0 for r in trained],
            width=0.36,
            label="Recall",
        )
        ax.axhline(0.7, color="C0", ls="--", label="Precision > 0.70")
        ax.axhline(0.5, color="C1", ls=":", label="Recall > 0.50")
        ax.set_xticks(
            positions,
            [r["name"].replace("Состояние ", "").replace("Датчик ", "") for r in trained],
            rotation=20,
        )
        ax.set_ylim(0, 1.04)
        for pos, item in zip(positions, trained, strict=True):
            if item["test"]["precision"] is None or item["test"]["recall"] is None:
                ax.text(
                    pos, 0.025, "не определены", ha="center", va="bottom", fontsize=8, rotation=90
                )
        ax.set_title("each_channel: отложенная проверка 1 января — 2 мая 2026")
        ax.legend(loc="upper right", ncols=2)
        ax.grid(axis="y", alpha=0.2)
        fig.tight_layout()
        fig.savefig(folder / "figures/quality.png", dpi=150)
        plt.close(fig)
    print(f"Models: {len(trained)}, passed: {passed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=Path(__file__).resolve().parent)
    render(parser.parse_args().folder)
