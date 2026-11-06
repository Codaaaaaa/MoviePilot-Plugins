"""
不依赖 MoviePilot 的自检：用假的 app.* 模块和假的 Discord 交互跑 cog 逻辑。
运行：pip install discord.py && python tests/test_cog.py
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ---- 假的 MoviePilot 模块 ----
def stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class MediaType:
    MOVIE = "电影"
    TV = "电视剧"


stub("app")
stub("app.log", logger=MagicMock())
stub("app.chain")
stub("app.chain.search", SearchChain=MagicMock)
stub("app.chain.subscribe", SubscribeChain=MagicMock)
stub("app.chain.download", DownloadChain=MagicMock)
stub("app.core")
stub("app.core.context", MediaInfo=object)
stub("app.core.metainfo", MetaInfo=lambda title: types.SimpleNamespace(begin_season=None, title=title))
stub("app.db")
stub("app.db.subscribe_oper", SubscribeOper=MagicMock)
stub("app.schemas")
stub("app.schemas.types", MediaType=MediaType)

path = Path(__file__).parent.parent / "plugins.v2/discord/cogs/moviepilot_cog.py"
spec = importlib.util.spec_from_file_location("cog", path)
cog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cog)


def media(**kw):
    base = dict(title="沙丘", year="2021", title_year="沙丘 (2021)", type=MediaType.MOVIE, tmdb_id=1,
                overview=None, tmdb_info=None, homepage="", release_date=None, vote_average=0,
                backdrop_path=None, poster_path=None, seasons={})
    base.update(kw)
    return types.SimpleNamespace(**base)


def interaction():
    i = MagicMock()
    i.response.is_done.return_value = False
    i.response.edit_message = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    i.user.display_name = "小明"
    i.user.mention = "<@1>"
    return i


async def main():
    # 空字段不报错
    e = cog.media_embed(media())
    assert e.description == "暂无简介" and e.fields[0].value == "未知" and e.url is None

    # 订阅成功：按钮变灰并显示已订阅
    chain = MagicMock()
    chain.add.return_value = (1, "")
    view = cog.SubscribeView(chain, media(), types.SimpleNamespace(begin_season=None))
    btn = view.subscribe
    i = interaction()
    await btn.callback(i)
    assert btn.disabled and btn.label == "✅ 已订阅", btn.label
    chain.add.assert_called_once()

    # 订阅失败 / 抛异常：按钮恢复，可重试
    for ret in [(None, "未识别到媒体信息"), RuntimeError("boom")]:
        chain = MagicMock()
        chain.add.side_effect = ret if isinstance(ret, Exception) else None
        chain.add.return_value = ret
        view = cog.SubscribeView(chain, media(), types.SimpleNamespace(begin_season=None))
        i = interaction()
        await view.subscribe.callback(i)
        assert not view.subscribe.disabled and view.subscribe.label == "订阅"
        assert "失败" in i.followup.send.call_args.args[0]

    # 处理中再点：不会重复订阅
    view = cog.SubscribeView(MagicMock(), media(), types.SimpleNamespace(begin_season=None))
    view.busy = True
    i = interaction()
    await view.subscribe.callback(i)
    view.subscribechain.add.assert_not_called()

    # 超时：按钮全部变灰
    view = cog.SubscribeView(MagicMock(), media(), types.SimpleNamespace(begin_season=None))
    view.message = MagicMock(edit=AsyncMock())
    await view.on_timeout()
    assert all(c.disabled for c in view.children)
    view.message.edit.assert_awaited()

    # 指令：没结果给提示；多结果最多 3 条
    c = cog.MPCog(MagicMock())
    c.searchchain.search_medias = MagicMock(return_value=[])
    i = interaction()
    await c.subscribe.callback(c, i, "不存在的片")
    assert "没有找到" in i.followup.send.call_args.args[0]
    c.searchchain.search_medias = MagicMock(return_value=[media(tmdb_id=n) for n in range(5)])
    i = interaction()
    await c.subscribe.callback(c, i, "沙丘")
    assert i.followup.send.await_count == 3

    # 出错时给用户提示
    i = interaction()
    await c.cog_app_command_error(i, RuntimeError("boom"))
    assert "出错了" in i.response.send_message.call_args.args[0]

    print("all ok")


asyncio.run(main())
