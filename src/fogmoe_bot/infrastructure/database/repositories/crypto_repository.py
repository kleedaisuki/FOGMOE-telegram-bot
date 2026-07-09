from fogmoe_bot.infrastructure.database import mysql_connection


async def upsert_group_chart_token(
    group_id: int,
    chain: str,
    ca: str,
    set_by: int,
    *,
    connection=None,
) -> None:
    """@brief 写入群组图表代币 / Upsert a group's chart token.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param chain 区块链标识 / Chain identifier.
    @param ca 合约地址 / Contract address.
    @param set_by 设置者用户 ID / Setter user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO group_chart_tokens (group_id, chain, ca, set_by) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE chain = VALUES(chain), ca = VALUES(ca), set_by = VALUES(set_by)",
        (group_id, chain, ca, set_by),
        connection=connection,
    )


async def fetch_group_chart_token(group_id: int, *, connection=None):
    """@brief 读取群组图表代币 / Fetch a group's chart token.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(chain, ca)` 行；不存在时返回 None / `(chain, ca)` row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT chain, ca FROM group_chart_tokens WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )


async def delete_group_chart_token(group_id: int, *, connection=None) -> int:
    """@brief 删除群组图表代币 / Delete a group's chart token.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 删除行数 / Deleted row count.
    """

    return await mysql_connection.execute(
        "DELETE FROM group_chart_tokens WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )


async def count_pending_swap_requests(user_id: int, *, connection=None) -> int:
    """@brief 统计待处理兑换请求 / Count pending swap requests.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 待处理请求数量 / Pending request count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM token_swap_requests WHERE user_id = %s AND status = 'pending'",
        (user_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def fetch_latest_pending_swap_request(user_id: int, *, connection=None):
    """@brief 读取最近的待处理兑换请求 / Fetch latest pending swap request.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(amount, wallet_address, request_time)` 行；不存在时返回 None / Pending request row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT amount, wallet_address, request_time "
        "FROM token_swap_requests "
        "WHERE user_id = %s AND status = 'pending' "
        "ORDER BY request_time DESC LIMIT 1",
        (user_id,),
        connection=connection,
    )


async def insert_swap_request(
    user_id: int,
    username: str,
    wallet_address: str,
    amount: int,
    *,
    connection=None,
) -> None:
    """@brief 创建代币兑换请求 / Insert a token swap request.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param username Telegram 用户名 / Telegram username.
    @param wallet_address 收款钱包地址 / Recipient wallet address.
    @param amount 兑换金币数量 / Swap amount.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO token_swap_requests (user_id, username, wallet_address, amount) "
        "VALUES (%s, %s, %s, %s)",
        (user_id, username, wallet_address, amount),
        connection=connection,
    )


async def fetch_active_btc_prediction(user_id: int, now, *, connection=None):
    """@brief 读取活跃 BTC 预测 / Fetch active BTC prediction.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param now 当前时间 / Current time.
    @param connection 可选数据库连接 / Optional database connection.
    @return 活跃预测行；不存在时返回 None / Active prediction row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT predict_type, amount, start_price, start_time, end_time "
        "FROM user_btc_predictions "
        "WHERE user_id = %s AND is_completed = FALSE AND end_time > %s",
        (user_id, now),
        connection=connection,
    )


async def fetch_uncompleted_btc_prediction(user_id: int, *, connection=None):
    """@brief 读取未完成 BTC 预测 / Fetch uncompleted BTC prediction.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 未完成预测行；不存在时返回 None / Uncompleted prediction row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT user_id, predict_type, amount, start_price, end_time "
        "FROM user_btc_predictions WHERE user_id = %s AND is_completed = FALSE",
        (user_id,),
        connection=connection,
    )


async def fetch_uncompleted_btc_prediction_result(user_id: int, *, connection=None):
    """@brief 读取待结算 BTC 预测 / Fetch BTC prediction result input.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(predict_type, amount, start_price)` 行；不存在时返回 None / Result input row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT predict_type, amount, start_price FROM user_btc_predictions "
        "WHERE user_id = %s AND is_completed = FALSE",
        (user_id,),
        connection=connection,
    )


async def complete_btc_predictions(user_id: int, *, connection=None) -> None:
    """@brief 标记用户 BTC 预测完成 / Mark a user's BTC predictions completed.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE user_btc_predictions SET is_completed = TRUE WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )


async def complete_active_btc_prediction(user_id: int, *, connection=None) -> None:
    """@brief 标记用户当前 BTC 预测完成 / Mark a user's active BTC prediction completed.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE user_btc_predictions SET is_completed = TRUE "
        "WHERE user_id = %s AND is_completed = FALSE",
        (user_id,),
        connection=connection,
    )


async def replace_btc_prediction(
    user_id: int,
    predict_type: str,
    amount: int,
    start_price: float,
    start_time,
    end_time,
    *,
    connection=None,
) -> None:
    """@brief 替换用户 BTC 预测 / Replace a user's BTC prediction.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param predict_type 预测方向 / Prediction direction.
    @param amount 投入金额 / Stake amount.
    @param start_price 起始价格 / Start price.
    @param start_time 起始时间 / Start time.
    @param end_time 结束时间 / End time.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "DELETE FROM user_btc_predictions WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    await mysql_connection.execute(
        "INSERT INTO user_btc_predictions "
        "(user_id, predict_type, amount, start_price, start_time, end_time) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, predict_type, amount, start_price, start_time, end_time),
        connection=connection,
    )
