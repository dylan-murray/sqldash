from starlette.convertors import Convertor, register_url_convertor


class DashboardNameConvertor(Convertor):
    regex = "[^/]+(?:/[^/]+)?"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return str(value)


register_url_convertor("dname", DashboardNameConvertor())
