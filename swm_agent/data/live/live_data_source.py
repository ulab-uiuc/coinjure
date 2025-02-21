from data.data_source import DataSource


class LiveDataSource(DataSource):
    async def get_next_event(self) -> None:
        return None
