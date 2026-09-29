"""Внешние открытые наборы (этап 14, docs/architecture/external-data.md).

Реестр — `config/external_datasets.yaml`. `download` качает файлы набора в `data/external/<ключ>/raw/` и сверяет
sha256, `prepare` пишет `data/external/<ключ>/prepared/`: справочник, журнал в формате эмулятора, разметку и отчёт.
Загрузку в БД делает `load-external`.
"""
