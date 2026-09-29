// The daily routing table of lct-25 (`ml/models/daily_routing.json`, 2026-09-29) as /models passes it (task C):
// sensor type → daily model, why, and its offline result on the rows common with v0; shares rounded.

import type { DailyRouting } from '@/lib/api/types'

export const DAILY_ROUTING: DailyRouting = {
  "version": "2026-09-29",
  "sha256": null,
  "rule": "модель ML-разработчика, если на общих с v0 строках P > 0.7, R > 0.5 и не хуже v0; иначе v0 (A21)",
  "report": "ml/reports/daily-routing/REPORT.md",
  "test": {
    "from": "2026-01-01",
    "to": "2026-05-02",
    "label": "I полугодие 2026: T с 1.01 по 2.05, горизонт до 1.06"
  },
  "routes": [
    {
      "sensor_type": "Датчик дыма",
      "model": "baseline-v0",
      "why": "его модель не проходит P > 0.7 и R > 0.5 на общих строках",
      "offline": {
        "decisions": 441067,
        "positives": 17679,
        "alerts": 2,
        "tp": 0,
        "fp": 2,
        "precision": 0.0,
        "recall": 0.0,
        "lead_days": null,
        "episodes": 1206,
        "caught_episodes": 0
      }
    },
    {
      "sensor_type": "Состояние насоса",
      "model": "baseline-v0",
      "why": "его совет — оставить v0: R 95.7% против 83.0% у его модели",
      "offline": {
        "decisions": 19748,
        "positives": 15935,
        "alerts": 18739,
        "tp": 15258,
        "fp": 3481,
        "precision": 0.8142,
        "recall": 0.9575,
        "lead_days": {
          "p10": 1.77,
          "p50": 7.93,
          "p90": 22.41
        },
        "episodes": 1996,
        "caught_episodes": 1973
      }
    },
    {
      "sensor_type": "Состояние вентилятора",
      "model": "baseline-v0",
      "why": "его совет — оставить v0: R 93.1% против 78.2% у его модели",
      "offline": {
        "decisions": 35606,
        "positives": 20818,
        "alerts": 24006,
        "tp": 19371,
        "fp": 4635,
        "precision": 0.8069,
        "recall": 0.9305,
        "lead_days": {
          "p10": 1.79,
          "p50": 8.43,
          "p90": 22.91
        },
        "episodes": 2400,
        "caught_episodes": 2344
      }
    },
    {
      "sensor_type": "Датчик температуры",
      "model": "baseline-v0",
      "why": "его модель не проходит P > 0.7 и R > 0.5 на общих строках",
      "offline": {
        "decisions": 66746,
        "positives": 69,
        "alerts": 0,
        "tp": 0,
        "fp": 0,
        "precision": null,
        "recall": 0.0,
        "lead_days": null,
        "episodes": 20,
        "caught_episodes": 0
      }
    },
    {
      "sensor_type": "Газовый датчик",
      "model": "baseline-v0",
      "why": "его модель не проходит P > 0.7 и R > 0.5 на общих строках",
      "offline": {
        "decisions": 42870,
        "positives": 1853,
        "alerts": 0,
        "tp": 0,
        "fp": 0,
        "precision": null,
        "recall": 0.0,
        "lead_days": null,
        "episodes": 89,
        "caught_episodes": 0
      }
    },
    {
      "sensor_type": "Состояние фазы",
      "model": "baseline-v0",
      "why": "другой модели нет",
      "offline": {
        "decisions": 98079,
        "positives": 14445,
        "alerts": 7879,
        "tp": 4695,
        "fp": 3184,
        "precision": 0.5959,
        "recall": 0.325,
        "lead_days": {
          "p10": 2.3,
          "p50": 9.11,
          "p90": 23.15
        },
        "episodes": 1087,
        "caught_episodes": 572
      }
    },
    {
      "sensor_type": "Ручной извещатель",
      "model": "manual_alarm",
      "why": "его модель проходит пороги на общих строках, v0 — нет",
      "offline": {
        "decisions": 12468,
        "positives": 3469,
        "alerts": 2562,
        "tp": 1795,
        "fp": 767,
        "precision": 0.7006,
        "recall": 0.5174,
        "lead_days": {
          "p10": 2.43,
          "p50": 11.47,
          "p90": 23.64
        },
        "episodes": 301,
        "caught_episodes": 235
      }
    },
    {
      "sensor_type": "ИБП",
      "model": "ups_ensemble",
      "why": "его модель проходит пороги на общих строках, v0 — нет",
      "offline": {
        "decisions": 8266,
        "positives": 388,
        "alerts": 329,
        "tp": 255,
        "fp": 74,
        "precision": 0.7751,
        "recall": 0.6572,
        "lead_days": {
          "p10": 2.44,
          "p50": 7.49,
          "p90": 14.2
        },
        "episodes": 27,
        "caught_episodes": 22
      }
    }
  ],
  "unrouted": [
    {
      "sensor_type": "Тепловой датчик",
      "why": "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    },
    {
      "sensor_type": "Датчик движения",
      "why": "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    },
    {
      "sensor_type": "КД Дверь",
      "why": "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    },
    {
      "sensor_type": "КД Люк",
      "why": "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    },
    {
      "sensor_type": "КД АВ",
      "why": "v0 тип не оценивает; его модель не проходит пороги — ежедневного прогноза у типа нет"
    }
  ],
  "offline": {
    "routing": {
      "decisions": 731945,
      "positives": 75605,
      "alerts": 53763,
      "tp": 41619,
      "fp": 12144,
      "precision": 0.7741,
      "recall": 0.5505,
      "lead_days": {
        "p10": 1.83,
        "p50": 8.42,
        "p90": 22.65
      },
      "episodes": 7201,
      "caught_episodes": 5177
    },
    "v0_only": {
      "decisions": 731642,
      "positives": 75313,
      "alerts": 50628,
      "tp": 39326,
      "fp": 11302,
      "precision": 0.7768,
      "recall": 0.5222,
      "lead_days": {
        "p10": 1.82,
        "p50": 8.38,
        "p90": 22.67
      },
      "episodes": 7172,
      "caught_episodes": 4891
    }
  }
}
