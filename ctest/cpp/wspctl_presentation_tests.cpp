/**
 * @file wspctl_presentation_tests.cpp
 * @brief presentation Unix gateway 单元测试 / Presentation Unix gateway unit tests.
 */

#include "wspctl/presentation/unix_gateway.hpp"

#include <cstdlib>
#include <iostream>
#include <string>
#include <sys/un.h>

namespace {

/** @brief 测试失败计数 / Test failure count. */
unsigned int g_failures{0U};

/**
 * @brief 断言一个条件 / Assert one condition.
 * @param condition 待断言条件 / Condition to assert.
 * @param message 失败说明 / Failure description.
 */
void expect(const bool condition, const std::string& message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 测试 AF_UNIX endpoint 的边界验证 / Test AF_UNIX endpoint boundary validation.
 * @note embedded NUL 必须在连接前拒绝，否则 `sockaddr_un::sun_path` 的 C-string 拷贝会截断。
 *       An embedded NUL must be rejected before connection, otherwise C-string copying into
 *       `sockaddr_un::sun_path` would truncate it.
 */
void test_socket_path_validation() {
    expect(
        wspctl::presentation::UnixGatewayClient::validate_socket_path("/run/wspctl/broker.sock").has_value(),
        "accept an absolute AF_UNIX endpoint");
    expect(
        !wspctl::presentation::UnixGatewayClient::validate_socket_path("").has_value(),
        "reject an empty endpoint");
    expect(
        !wspctl::presentation::UnixGatewayClient::validate_socket_path("relative.sock").has_value(),
        "reject a relative endpoint");
    /** @brief 含 NUL 的 endpoint / Endpoint containing a NUL. */
    std::string embedded_nul{"/run/wspctl/"};
    embedded_nul.push_back('\0');
    embedded_nul += "broker.sock";
    expect(
        !wspctl::presentation::UnixGatewayClient::validate_socket_path(embedded_nul).has_value(),
        "reject an endpoint with an embedded NUL");
    /** @brief 刚好达到 sun_path 上限的 endpoint / Endpoint exactly at the sun_path limit. */
    std::string too_long(sizeof(sockaddr_un::sun_path), 'x');
    too_long.front() = '/';
    expect(
        !wspctl::presentation::UnixGatewayClient::validate_socket_path(too_long).has_value(),
        "reject an endpoint at the sun_path limit");
}

}  // namespace

/**
 * @brief presentation CTest 入口 / Presentation CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_socket_path_validation();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
