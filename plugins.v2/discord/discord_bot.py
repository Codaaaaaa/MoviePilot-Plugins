import discord
from discord.ext import commands
import asyncio
import app.plugins.discord.tokenes as tokenes
from app.log import logger

intents = discord.Intents.all()
client = commands.Bot(command_prefix='$', intents=intents)

async def load_extensions():
    # 检查是否已加载，避免重复加载报错
    if "app.plugins.discord.cogs.moviepilot_cog" in client.extensions:
        logger.info("Cog 已经加载，跳过")
        return
    try:
        await client.load_extension("app.plugins.discord.cogs.moviepilot_cog")
        logger.info("Cog 加载完成")
    except Exception as e:
        logger.error(f"Cog 加载失败: {e}")

async def unload_extensions():
    if "app.plugins.discord.cogs.moviepilot_cog" not in client.extensions:
        return
    try:
        await client.unload_extension("app.plugins.discord.cogs.moviepilot_cog")
        logger.info("Cog 卸载完成")
    except Exception as e:
        logger.error(f"Cog 卸载失败: {e}")

async def run_bot():
    if tokenes.is_bot_running:
        logger.info("Discord bot 已经在运行")
        return

    try:
        logger.info("Discord bot 启动中...")
        tokenes.is_bot_running = True
        await load_extensions()
        asyncio.ensure_future(client.start(tokenes.bot_token))
    except Exception as e:
        logger.error(f"Discord bot 启动失败: {e}")
        tokenes.is_bot_running = False

async def stop():
    logger.info(f"is bot running: {tokenes.is_bot_running}")
    if not tokenes.is_bot_running:
        logger.info("Discord bot 未运行")
        return

    try:
        logger.info("Discord bot 停止中...")
        await unload_extensions()
        tokenes.is_bot_running = False
        await client.close()
    except Exception as e:
        logger.error(f"Discord bot 停止失败: {e}")