"""The one pagination convention (01): {items, total, limit, offset}.

async def get_list(self, page: Page = Depends(page_params), session=Depends(get_db)):
    q = select(Order).limit(page.limit).offset(page.offset)
    return paginated(items, total, page)
"""

from dataclasses import dataclass

from fastapi import Query

MAX_LIMIT = 200


@dataclass
class Page:
    limit: int
    offset: int


def page_params(limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)) -> Page:
    return Page(limit=limit, offset=offset)


def paginated(items: list, total: int, page: Page) -> dict:
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}
