#pragma once

namespace wspctl::detail {

/**
 * @brief 以 pidfd 发送信号并消费其所有权 / Send a signal through a pidfd and consume its ownership.
 * @param owned_pidfd 由调用方拥有的 pidfd；无论成功与否都会被关闭并置为 ``-1`` /
 *        Caller-owned pidfd; it is closed and reset to ``-1`` regardless of success.
 * @param signal 要发送的 POSIX signal / POSIX signal to send.
 * @return 已送达 signal，或 target 已退出（``ESRCH``）时为真 / True when the signal was delivered or the target had already exited (``ESRCH``).
 * @note 此函数不会重试 ``close(2)``：Linux 上重试一个被 ``EINTR`` 中断的 close 可能关闭已复用的
 *       descriptor。 This function never retries ``close(2)``: on Linux, retrying a close
 *       interrupted by ``EINTR`` can close a reused descriptor.
 */
[[nodiscard]] bool signal_and_close_pidfd(int& owned_pidfd, int signal) noexcept;

}  // namespace wspctl::detail
