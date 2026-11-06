import asyncio
import traceback
from typing import List

import discord
from discord import app_commands
from discord.ext import commands
from app.log import logger
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo


async def reply(interaction: discord.Interaction, content: str, **kwargs):
    """无论交互是否已响应都能发出消息；交互已失效时只记日志"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    except discord.HTTPException as e:
        logger.error(f"Discord 回复失败: {e}")


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
    @app_commands.describe(title="片名，剧集可带季数，如：庆余年 第二季")
    async def subscribe(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()
        meta = MetaInfo(title=title)
        # MoviePilot 的链路是同步阻塞的，放到线程里跑，避免卡住 bot
        medias = await asyncio.to_thread(self.searchchain.search_medias, meta=meta)
        if not medias:
            await interaction.followup.send(f"没有找到「{title}」，换个关键词试试？")
            return
        # 所有结果放在一条消息里：默认展示第一个，下拉框切换
        view = SubscribeView(self.subscribechain, medias[:10], meta)
        content = f"找到 {len(view.medias)} 个结果，可在下拉框中切换" if len(view.medias) > 1 else None
        view.message = await interaction.followup.send(content=content, embed=media_embed(view.media),
                                                       view=view, wait=True)


class SubscribeView(discord.ui.View):

    def __init__(self, subscribechain: SubscribeChain, medias: List[MediaInfo], meta):
        super().__init__(timeout=180)
        self.subscribechain = subscribechain
        self.medias = medias
        self.index = 0
        self.meta = meta
        self.message = None
        self.busy = False
        if len(medias) > 1:
            self.picker = discord.ui.Select(placeholder="选择其他结果", row=0, options=[
                discord.SelectOption(label=m.title_year[:100], value=str(n), default=n == 0,
                                     description=(m.overview or "")[:100] or None)
                for n, m in enumerate(medias)
            ])
            self.picker.callback = self.pick
            self.add_item(self.picker)

    @property
    def media(self) -> MediaInfo:
        return self.medias[self.index]

    async def pick(self, interaction: discord.Interaction):
        self.index = int(self.picker.values[0])
        for option in self.picker.options:
            option.default = option.value == self.picker.values[0]
        await interaction.response.edit_message(embed=media_embed(self.media), view=self)

    def lock(self, locked: bool):
        for item in self.children:
            item.disabled = locked

    @discord.ui.button(label="订阅", style=discord.ButtonStyle.blurple, row=1)
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
                tmdbid=media.tmdb_id,
                season=self.meta.begin_season,
                exist_ok=True,
                username="Discord Bot"
            )
        except Exception as e:
            log_error("订阅", e)
            sid, err = None, str(e)

        if sid:
            button.label = "✅ 已订阅"
            button.style = discord.ButtonStyle.green
            self.stop()
            await interaction.edit_original_response(content=f"✅ 已添加订阅 {media.title_year}", view=self)
        else:
            self.busy = False
            self.lock(False)
            button.label = "订阅"
            await interaction.edit_original_response(view=self)
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
