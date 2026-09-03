from jiffle.configuration.settings import Settings
from jiffle.features.imports.source_adapters.danbooru import DanbooruSourceProvider
from jiffle.features.imports.source_adapters.e621 import E621SourceProvider
from jiffle.features.imports.source_adapters.furaffinity import FurAffinitySourceProvider
from jiffle.features.imports.source_adapters.gelbooru import GelbooruSourceProvider
from jiffle.features.imports.source_adapters.tbib import TbibSourceProvider


def build_source_providers(settings: Settings):
    return (
        DanbooruSourceProvider(settings.danbooru_login, settings.danbooru_api_key),
        E621SourceProvider(settings.e621_login, settings.e621_api_key),
        GelbooruSourceProvider(settings.gelbooru_user_id, settings.gelbooru_api_key),
        FurAffinitySourceProvider(settings.furaffinity_cookie_a, settings.furaffinity_cookie_b),
        TbibSourceProvider(),
    )
