#include "wspctl/infrastructure/detail/pidfd_control.hpp"

#include <cerrno>
#include <sys/syscall.h>
#include <unistd.h>

#include <utility>

namespace wspctl::detail {

bool signal_and_close_pidfd(int& owned_pidfd, const int signal) noexcept {
    /** @brief 先转移所有权，避免任何失败路径泄漏 descriptor / Transfer ownership first so no failure path leaks the descriptor. */
    const int pidfd = std::exchange(owned_pidfd, -1);
    if (pidfd < 0) {
        return false;
    }
    /** @brief kernel 是否确认 target terminal / Whether the kernel confirmed the target is terminal. */
    bool terminal = false;
#ifdef SYS_pidfd_send_signal
    if (syscall(SYS_pidfd_send_signal, pidfd, signal, nullptr, 0U) == 0) {
        terminal = true;
    } else if (errno == ESRCH) {
        // ESRCH through a pidfd has no PID-reuse ambiguity: the exact target is already gone.
        terminal = true;
    }
#else
    static_cast<void>(signal);
#endif
    // Do not retry close after EINTR; descriptor reuse makes such a retry unsafe on Linux.
    static_cast<void>(close(pidfd));
    return terminal;
}

}  // namespace wspctl::detail
