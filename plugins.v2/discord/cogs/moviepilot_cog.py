import asyncio
import traceback

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
        for media in medias[:3]:
            view = SubscribeView(self.subscribechain, media, meta)
            view.message = await interaction.followup.send(embed=media_embed(media), view=view, wait=True)


class SubscribeView(discord.ui.View):

    def __init__(self, subscribechain: SubscribeChain, media: MediaInfo, meta):
        super().__init__(timeout=180)
        self.subscribechain = subscribechain
        self.media = media
        self.meta = meta
        self.message = None
        self.busy = False

    @discord.ui.button(label="订阅", style=discord.ButtonStyle.blurple)
    async def subscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 先锁住按钮，防止重复点击
        if self.busy:
            await interaction.response.send_message("正在处理中，请稍候", ephemeral=True)
            return
        self.busy = True
        button.disabled = True
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
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(f"已添加订阅 {media.title_year}")
        else:
            self.busy = False
            button.disabled = False
            button.label = "订阅"
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(f"❌ 订阅 {media.title_year} 失败：{err}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        log_error("按钮", error)
        await reply(interaction, f"❌ 出错了：{error}", ephemeral=True)

    async def on_timeout(self):
        # 超时后按钮变灰，而不是看起来能点、点了却报"交互失败"
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(MPCog(bot))
