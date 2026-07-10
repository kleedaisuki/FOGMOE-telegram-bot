import logging
import os
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from sqlalchemy.exc import SQLAlchemyError
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database.repositories import moderation_repository
from fogmoe_bot.infrastructure.logging.bot_logging import current_log_file_path
import tempfile
from fogmoe_bot.application.telegram.command_cooldown import cooldown # 导入冷却装饰器

# 定义开发者命令处理函数

@cooldown # 添加冷却装饰器
async def get_bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示机器人当前服务的部分统计信息和群组ID列表"""
    
    # 检查使用者是否为管理员
    if update.effective_user.id != config.ADMIN_USER_ID: # ADMIN_USER_ID
        await update.message.reply_text("您没有权限执行此操作")
        return
    
    try:
        limit = 20
        stats = await moderation_repository.fetch_developer_stats(limit)
        
        # --- 构建统计信息消息 ---
        stats_message = f"🤖 *机器人统计信息*\n\n"
        stats_message += f"👤 总用户数: {stats['user_count']}\n"
        stats_message += f"💬 配置关键词群组: {stats['keyword_group_count']}\n"
        stats_message += f"✅ 启用验证群组: {stats['verify_group_count']}\n"
        stats_message += f"🛡️ 启用垃圾控制群组: {stats['spam_group_count']}\n"
        stats_message += f"📈 配置图表群组: {stats['chart_group_count']}\n\n"
        
        # 添加最近用户信息
        stats_message += "*最近的用户 (按ID排序，最多10个):*\n"
        if stats["recent_users"]:
            for user in stats["recent_users"]:
                # 使用数据库中的 'name' 字段
                user_info = f"ID: {user['id']}, Name: {user['name']}"
                stats_message += f"- {user_info}\n"
        else:
            stats_message += "无\n"

        # 添加群组 ID 列表
        stats_message += f"\n*使用各项功能的群组 ID (最多{limit}个):*\n"
        stats_message += f"💬 关键词: `{', '.join(stats['keyword_group_ids']) if stats['keyword_group_ids'] else '无'}`\n"
        stats_message += f"✅ 验证: `{', '.join(stats['verify_group_ids']) if stats['verify_group_ids'] else '无'}`\n"
        stats_message += f"🛡️ 垃圾控制: `{', '.join(stats['spam_group_ids']) if stats['spam_group_ids'] else '无'}`\n"
        stats_message += f"📈 图表: `{', '.join(stats['chart_group_ids']) if stats['chart_group_ids'] else '无'}`\n"

        # 发送消息 (如果太长可能需要分段或发文件)
        if len(stats_message) > 4000:
            await update.message.reply_text("统计信息过长，将以文件形式发送。")
            # 可以考虑将 stats_message 写入临时文件发送
            try:
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.md', delete=False, encoding='utf-8') as temp_file:
                    temp_file.write(stats_message)
                    temp_file_path = temp_file.name
                with open(temp_file_path, 'rb') as f:
                    await update.message.reply_document(document=f, filename="bot_stats.md")
                os.remove(temp_file_path)
            except Exception as file_e:
                logging.error(f"发送统计文件出错: {file_e}")
                await update.message.reply_text("发送统计文件时出错。")
        else:
           await update.message.reply_text(stats_message, parse_mode='Markdown')
        
    except SQLAlchemyError as db_err:
        logging.error(f"数据库查询出错: {str(db_err)}")
        await update.message.reply_text(f"数据库查询出错: {str(db_err)}")
    except Exception as e:
        logging.error(f"获取统计信息出错: {str(e)}")
        await update.message.reply_text(f"获取统计信息出错: {str(e)}")

@cooldown # 添加冷却装饰器
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示机器人最近的日志"""
    
    # 检查使用者是否为管理员
    if update.effective_user.id != config.ADMIN_USER_ID:  # ADMIN_USER_ID
        await update.message.reply_text("您没有权限执行此操作")
        return
    
    try:
        # 获取日志行数参数，默认为50行
        lines = 50
        if context.args and context.args[0].isdigit():
            lines = min(int(context.args[0]), 200)  # 限制最多显示200行
        
        # 读取日志文件的最后N行
        log_path = current_log_file_path()
        if not os.path.exists(log_path):
            await update.message.reply_text("日志文件不存在")
            return
        
        # 读取最后N行日志
        with open(log_path, 'r', encoding='utf-8') as f:
            log_lines = f.readlines()
            last_logs = log_lines[-lines:] if len(log_lines) > lines else log_lines
        
        # 构建日志消息
        logs_message = f"📋 *最近{len(last_logs)}行日志*\n\n```\n"
        logs_message += ''.join(last_logs)
        logs_message += "\n```"
        
        # 如果日志太长，分段发送或发文件
        if len(logs_message) > 4000:
            await update.message.reply_text("日志内容过长，将以文件形式发送")
            try:
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False, encoding='utf-8') as temp_file:
                    temp_file.write("".join(last_logs))
                    temp_file_path = temp_file.name
                with open(temp_file_path, 'rb') as f:
                    await update.message.reply_document(document=f, filename="bot_logs.log")
                os.remove(temp_file_path)
            except Exception as file_e:
                logging.error(f"发送日志文件出错: {file_e}")
                await update.message.reply_text("发送日志文件时出错。")
        else:
            await update.message.reply_text(logs_message, parse_mode='Markdown')
            
    except Exception as e:
        logging.error(f"获取日志出错: {str(e)}")
        await update.message.reply_text(f"获取日志出错: {str(e)}")

# 设置开发者命令处理器
def setup_developer_handlers(application):
    """设置开发者命令处理器"""
    application.add_handler(CommandHandler("stats", get_bot_stats))
    application.add_handler(CommandHandler("logs", view_logs))
    logging.info("开发者命令模块已加载")
