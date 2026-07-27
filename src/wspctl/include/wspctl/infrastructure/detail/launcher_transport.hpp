#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <array>
#include <cstddef>
#include <span>
#include <vector>

namespace wspctl::detail::launcher_transport {

/** @brief fork-server 单包 wire 载荷上限 / Fork-server single-packet wire payload limit. */
inline constexpr std::size_t kMaxPacketBytes{32U * 1024U};

/** @brief fork-server 单包允许的最大 SCM_RIGHTS FD 数 / Maximum SCM_RIGHTS FD count per fork-server packet. */
inline constexpr std::size_t kMaxFileDescriptors{5U};

/**
 * @brief fork-server 的单包与其拥有的 FD / One fork-server packet and its owned FDs.
 *
 * @note 这是 broker 与单线程 fork-server 的私有 transport ABI，不是对 Python 或外部 client 的
 *       公共协议。 This is a private transport ABI between the broker and its single-threaded
 *       fork server, not a public protocol for Python or external clients.
 */
struct LauncherPacket final {
    /** @brief 单个 wire 数据报 / One wire datagram. */
    std::vector<std::byte> bytes;
    /** @brief 由 SCM_RIGHTS 接收且由本对象拥有的 FD / FDs received through SCM_RIGHTS and owned by this object. */
    std::array<int, kMaxFileDescriptors> fds = [] {
        std::array<int, kMaxFileDescriptors> descriptors{};
        descriptors.fill(-1);
        return descriptors;
    }();
    /** @brief `fds` 中有效的前缀长度 / Length of the valid prefix in `fds`. */
    std::size_t fd_count{};
};

/**
 * @brief 关闭并清空 packet 拥有的全部 FD / Close and invalidate every FD owned by a packet.
 * @param packet 待清理 packet / Packet to clean up.
 */
void close_launcher_packet_fds(LauncherPacket& packet) noexcept;

/**
 * @brief 通过一个 SOCK_SEQPACKET 发送 wire 与严格有界的 FD 集 / Send wire and a strictly bounded FD set through SOCK_SEQPACKET.
 * @param fd 已连接的 fork-server socket / Connected fork-server socket.
 * @param bytes 非空且有界的 wire bytes / Non-empty bounded wire bytes.
 * @param fds 待传递的 0 到 5 个 FD / Zero to five FDs to pass.
 * @return 成功或可恢复 transport 错误 / Success or a recoverable transport error.
 */
[[nodiscard]] Result<void> send_launcher_packet(
    int fd,
    std::span<const std::byte> bytes,
    std::span<const int> fds);

/**
 * @brief 从一个 SOCK_SEQPACKET 接收 wire 与严格有界的 FD 集 / Receive wire and a strictly bounded FD set from SOCK_SEQPACKET.
 * @param fd 已连接的 fork-server socket / Connected fork-server socket.
 * @return 拥有 FD 的 packet 或 fail-closed transport 错误 / FD-owning packet or a fail-closed transport error.
 */
[[nodiscard]] Result<LauncherPacket> receive_launcher_packet(int fd);

}  // namespace wspctl::detail::launcher_transport
