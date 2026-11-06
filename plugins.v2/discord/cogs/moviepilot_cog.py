import asyncio
import inspect
import traceback
from typing import List

import discord
from discord import app_commands
from discord.ext import commands
from app.log import logger
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType


async def reply(interaction: discord.Interaction, content: str, **kwargs):
    """无论交互是否已响应都能发出消息；交互已失效时只记日志"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    except discord.HTTPException as e:
        logger.error(f"Discord 回复失败: {e}")


def media_identity(func, media: MediaInfo = None, tmdbid: int = None) -> dict:
    """
    MoviePilot v2 用 tmdbid 标识媒体，v3 改成 media_source + media_id（tmdbid 会被当成多余参数忽略或报错），
    按目标函数的签名选用对应的参数
    """
    if "tmdbid" in inspect.signature(func).parameters:
        return {"tmdbid": tmdbid or media.tmdb_id}
    if media is not None and getattr(media, "media_id", None):
        return {"media_source": media.media_source, "media_id": media.media_id}
    from app.schemas.types import MediaSource  # 只有 v3 有
    return {"media_source": MediaSource.TMDB, "media_id": str(tmdbid or media.tmdb_id)}


def log_error(where: str, error: Exception):
    logger.error(f"Discord {where} 出错: {error}\n{''.join(traceback.format_exception(error))}")


def media_embed(media: MediaInfo) -> discord.Embed:
    # Discord 不接受空字段值，简介最长 4096
    overview = media.overview or (media.tmdb_info or {}).get("overview") or "暂无简介"
    embed = discord.Embed(title=media.title_year, description=overview[:4000], url=media.homepage or None)
    embed.add_field(name="发布日期", value=media.release_date or "未知")
    embed.add_field(name="评分", value=media.vote_average or "暂无")
    if media.backdrop_path:
        embed.set_image(url=media.backdrop_path)
    return embed


class MPCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()

    # 监听ready事件，bot准备好后打印登录信息
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'bot 登录成功 - {self.bot.user}')
        game = discord.Game("看电影中...")
        await self.bot.change_presence(status=discord.Status.idle, activity=game)
        slash = await self.bot.tree.sync()
        logger.info(f"已载入 {len(slash)} 个指令")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        error = getattr(error, "original", error)
        log_error("指令", error)
        await reply(interaction, f"❌ 出错了：{error}", ephemeral=True)

    @app_commands.command(description="订阅电影或剧集")
    @app_commands.describe(title="片名，剧集可带季数，如：庆余年 第二季；输入时可从候选中直接选择")
    async def subscribe(self, interaction: discord.Interaction, title: str):
        # 搜索过程和选择面板只有发起人可见，频道里只留订阅成功的消息
        await interaction.response.defer(ephemeral=True, thinking=True)
        # MoviePilot 的链路是同步阻塞的，放到线程里跑，避免卡住 bot
        picked = parse_choice(title)
        if picked:
            # 从自动补全选的，直接按 TMDB ID 识别，不用再搜
            mtype, tmdbid, season = picked
            recognize = self.searchchain.recognize_media
            media = await asyncio.to_thread(recognize, mtype=mtype, **media_identity(recognize, tmdbid=tmdbid))
            medias = [media] if media else []
        else:
            season = MetaInfo(title=title).begin_season
            medias = await asyncio.to_thread(self.searchchain.search_medias, meta=MetaInfo(title=title))
        if not medias:
            await interaction.followup.send(f"没有找到「{title}」，换个关键词试试？")
            return
        # 所有结果放在一条消息里：默认展示第一个，下拉框切换
        view = SubscribeView(self.subscribechain, medias[:10], season)
        await view.load(season)
        content = f"找到 {len(view.medias)} 个结果，可在下拉框中切换" if len(view.medias) > 1 else None
        view.message = await interaction.followup.send(content=content, embed=view.render(),
                                                       view=view, ephemeral=True, wait=True)

    @subscribe.autocomplete("title")
    async def title_autocomplete(self, interaction: discord.Interaction, current: str):
        if len(current.strip()) < 2:
            return []
        # Discord 要求 3 秒内返回候选，超时就不给候选，用户照样可以直接回车
        try:
            medias = await asyncio.wait_for(
                asyncio.to_thread(self.searchchain.search_medias, meta=MetaInfo(title=current)), timeout=2.5)
        except Exception:
            return []
        season = MetaInfo(title=current).begin_season or ""
        return [
            app_commands.Choice(name=f"{m.title_year} · {m.type.value if m.type else '未知'}"[:100],
                                value=f"{CHOICE_PREFIX}{m.type.value if m.type else ''}:{m.tmdb_id}:{season}")
            for m in (medias or [])[:25] if m.tmdb_id
        ]


# 自动补全候选的值：tmdb:<类型>:<tmdbid>:<季>
CHOICE_PREFIX = "tmdb:"


def parse_choice(value: str):
    if not value.startswith(CHOICE_PREFIX):
        return None
    try:
        _, mtype, tmdbid, season = value.split(":")
        return MediaType(mtype) if mtype else None, int(tmdbid), int(season) if season else None
    except ValueError:
        return None


TYPE_NAME = {MediaType.MOVIE: "🎬 电影", MediaType.TV: "📺 剧集"}


def media_status(chain: SubscribeChain, media: MediaInfo, season: int = None):
    """
    补全剧集的季信息，并查询订阅和入库状态（同步，放线程里跑）
    :return: (完整媒体信息, 季号, 已订阅, 入库状态文字, 是否还需要订阅)
    """
    if media.type == MediaType.TV and not media.seasons:
        media = chain.recognize_media(mtype=media.type, **media_identity(chain.recognize_media, media)) or media
    if media.type == MediaType.TV:
        seasons = sorted(s for s in (media.seasons or {}) if s > 0)
        # 没指定季（或季号不存在）时默认最新一季
        if seasons and season not in seasons:
            season = seasons[-1]
    else:
        season = None
    try:
        exists = SubscribeOper().exists
        subscribed = exists(**media_identity(exists, media), season=season)
        exist_flag, no_exists = DownloadChain().get_no_exists_info(meta=MetaInfo(media.title), mediainfo=media)
        state, need = library_state(media, season, exist_flag, no_exists)
    except Exception as e:
        log_error("查询状态", e)
        subscribed, state, need = False, "状态未知", True
    return media, season, subscribed, state, need


def library_state(media: MediaInfo, season: int, exist_flag: bool, no_exists: dict):
    """根据 get_no_exists_info 的结果给出 (入库状态文字, 是否还需要订阅)"""
    if media.type == MediaType.TV and season and not (media.seasons or {}).get(season):
        return f"第 {season} 季未播出", True
    if exist_flag:
        return "✅ 已入库", False
    if media.type != MediaType.TV or not no_exists:
        return "未入库", True
    # no_exists: {媒体key: {季: NotExistMediaInfo}}，没列出的季就是齐全的；episodes 为空表示整季缺失
    info = next(iter(no_exists.values()), {}).get(season)
    if not info:
        return f"✅ 第 {season} 季已入库", False
    if info.episodes:
        return f"第 {season} 季缺 {len(info.episodes)} 集", True
    return f"第 {season} 季未入库", True


class SubscribeView(discord.ui.View):

    def __init__(self, subscribechain: SubscribeChain, medias: List[MediaInfo], season: int = None):
        super().__init__(timeout=180)
        self.subscribechain = subscribechain
        self.medias = medias
        self.index = 0
        # 用户在片名里指定的季，切换条目时以它为准
        self.wanted_season = season
        self.season = None
        self.subscribed = False
        self.state = ""
        self.need = True
        self.season_picker = None
        self.message = None
        self.busy = False
        if len(medias) > 1:
            self.picker = discord.ui.Select(placeholder="选择其他结果", row=0, options=[
                discord.SelectOption(label=f"{TYPE_NAME.get(m.type, '')} {m.title_year}"[:100], value=str(n),
                                     default=n == 0, description=(m.overview or "")[:100] or None)
                for n, m in enumerate(medias)
            ])
            self.picker.callback = self.pick
            self.add_item(self.picker)

    @property
    def media(self) -> MediaInfo:
        return self.medias[self.index]

    async def load(self, season: int = None):
        """查询当前条目的季、订阅、入库状态"""
        media, self.season, self.subscribed, self.state, self.need = await asyncio.to_thread(
            media_status, self.subscribechain, self.media, season)
        self.medias[self.index] = media

    def render(self) -> discord.Embed:
        """按当前状态刷新季选择框和订阅按钮，返回详情卡片"""
        media = self.media
        if self.season_picker:
            self.remove_item(self.season_picker)
            self.season_picker = None
        seasons = sorted(s for s in (media.seasons or {}) if s > 0) if media.type == MediaType.TV else []
        if len(seasons) > 1:
            self.season_picker = discord.ui.Select(placeholder="选择季", row=1, options=[
                discord.SelectOption(label=f"第 {s} 季", value=str(s), default=s == self.season,
                                     description=f"{len(media.seasons.get(s) or [])} 集")
                for s in seasons[-25:]
            ])
            self.season_picker.callback = self.pick_season
            self.add_item(self.season_picker)

        button = self.subscribe
        button.disabled = self.subscribed or not self.need
        if self.subscribed:
            button.label, button.style = "✅ 已订阅", discord.ButtonStyle.green
        elif not self.need:
            button.label, button.style = "已在媒体库", discord.ButtonStyle.grey
        else:
            button.label = f"订阅第 {self.season} 季" if self.season else "订阅"
            button.style = discord.ButtonStyle.blurple

        embed = media_embed(media)
        embed.add_field(name="类型", value=TYPE_NAME.get(media.type, "未知"))
        if self.season:
            embed.add_field(name="季", value=f"第 {self.season} 季（共 {len(seasons)} 季）")
        embed.add_field(name="状态", value=" · ".join(filter(None, ["🔔 已订阅" if self.subscribed else "", self.state])) or "未知")
        return embed

    async def switch(self, interaction: discord.Interaction, season: int = None):
        """切换条目或季：查状态可能超过 3 秒，先 defer 再更新"""
        if self.busy:
            await interaction.response.send_message("正在处理中，请稍候", ephemeral=True)
            return
        self.busy = True
        await interaction.response.defer()
        try:
            await self.load(season)
        finally:
            self.busy = False
        await interaction.edit_original_response(embed=self.render(), view=self)

    async def pick(self, interaction: discord.Interaction):
        self.index = int(self.picker.values[0])
        for option in self.picker.options:
            option.default = option.value == self.picker.values[0]
        await self.switch(interaction, self.wanted_season)

    async def pick_season(self, interaction: discord.Interaction):
        await self.switch(interaction, int(self.season_picker.values[0]))

    def lock(self, locked: bool):
        for item in self.children:
            item.disabled = locked

    @discord.ui.button(label="订阅", style=discord.ButtonStyle.blurple, row=2)
    async def subscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 先锁住整个面板，防止重复点击或订阅中途切换
        if self.busy:
            await interaction.response.send_message("正在处理中，请稍候", ephemeral=True)
            return
        self.busy = True
        self.lock(True)
        button.label = "订阅中..."
        await interaction.response.edit_message(view=self)

        media = self.media
        try:
            sid, err = await asyncio.to_thread(
                self.subscribechain.add,
                title=media.title,
                year=media.year,
                mtype=media.type,
                **media_identity(self.subscribechain.add, media),
                season=self.season,
                exist_ok=True,
                # MoviePilot 订阅记录里显示真实的 Discord 用户
                username=interaction.user.display_name
            )
        except Exception as e:
            log_error("订阅", e)
            sid, err = None, str(e)

        # 面板保持可用，可以接着订阅别的季或别的结果
        self.busy = False
        self.lock(False)
        if sid:
            self.subscribed = True
            season = f" 第 {self.season} 季" if self.season else ""
            done = f"✅ {interaction.user.display_name} 已订阅 {media.title_year}{season}"
            await interaction.edit_original_response(content=done, embed=self.render(), view=self)
            # 面板只有自己看得到，订阅结果公开发到频道
            await interaction.followup.send(done)
        else:
            await interaction.edit_original_response(embed=self.render(), view=self)
            await interaction.followup.send(f"❌ 订阅 {media.title_year} 失败：{err}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        log_error("按钮", error)
        await reply(interaction, f"❌ 出错了：{error}", ephemeral=True)

    async def on_timeout(self):
        # 超时后按钮变灰，而不是看起来能点、点了却报"交互失败"
        self.lock(True)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(MPCog(bot))
