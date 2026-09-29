"""Общие параметры запросов: пагинация `?page=&size=`, списки через запятую (docs/api.md, «Общее»)."""

MAX_PAGE_SIZE = 500


def page_params(page: int, size: int) -> tuple[int, int]:
    return max(1, page), min(MAX_PAGE_SIZE, max(1, size))


def csv_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]
