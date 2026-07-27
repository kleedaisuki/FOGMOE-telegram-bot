#include "wspctl/infrastructure/image.hpp"

#include <cstdio>
#include <filesystem>
#include <string>
#include <string_view>

/**
 * @brief wspctl-image 只读 verifier 入口 / wspctl-image read-only verifier entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数 / Arguments.
 * @return POSIX 退出码 / POSIX exit code.
 * @note 镜像生成属于受控构建流水线；该工具只验证 generation、manifest 和内容摘要。
 *       Image creation belongs to a controlled build pipeline; this tool only verifies generation, manifest, and content digest.
 */
int main(const int argc, char *argv[])
{
    /** @brief 待验证或 seal 的 rootfs / Rootfs to verify or seal. */
    std::filesystem::path base_root;
    /** @brief 已发布 images 根（仅 verify） / Published images root (verify only). */
    std::filesystem::path images_root;
    /** @brief seal 目标 generation（仅 seal） / Target generation to seal (seal only). */
    std::string generation;
    /** @brief 是否选择 verify 模式 / Whether verify mode was selected. */
    bool verify = false;
    /** @brief 是否选择 seal 模式 / Whether seal mode was selected. */
    bool seal = false;
    for (int index = 1; index < argc;)
    {
        /** @brief 当前命令行选项 / Current command-line option. */
        const std::string_view option{argv[index]};
        if (option == "--seal")
        {
            if (seal)
            {
                std::fputs("wspctl-image: duplicate --seal\n", stderr);
                return 64;
            }
            seal = true;
            ++index;
            continue;
        }
        if (index + 1 >= argc)
        {
            std::fputs("wspctl-image: option value missing\n", stderr);
            return 64;
        }
        /** @brief 当前命令行选项值 / Current command-line option value. */
        const std::string_view value{argv[index + 1]};
        if (option == "--verify")
        {
            verify = value == "true";
        }
        else if (option == "--base-root")
        {
            base_root = value;
        }
        else if (option == "--images-root")
        {
            images_root = value;
        }
        else if (option == "--generation")
        {
            generation = value;
        }
        else
        {
            std::fputs("wspctl-image: unknown option\n", stderr);
            return 64;
        }
        index += 2;
    }
    if (verify == seal)
    {
        std::fputs("wspctl-image: select exactly one of --verify true or --seal\n", stderr);
        return 64;
    }
    if (seal)
    {
        if (base_root.empty() || generation.empty() || !images_root.empty())
        {
            std::fputs("usage: wspctl-image --seal --base-root ABS --generation SAFE\n", stderr);
            return 64;
        }
        const auto manifest = wspctl::seal_image_root(base_root, generation);
        if (!manifest)
        {
            std::fputs("wspctl-image: image sealing failed\n", stderr);
            return 78;
        }
        std::printf("generation=%s\ndigest=%s\n", manifest->generation.c_str(), manifest->rootfs_digest.c_str());
        return 0;
    }
    if (!verify || base_root.empty() || images_root.empty() || !generation.empty())
    {
        std::fputs("usage: wspctl-image --verify true --base-root ABS --images-root ABS\n", stderr);
        return 64;
    }
    const auto manifest = wspctl::validate_image_root(base_root, images_root);
    if (!manifest)
    {
        std::fputs("wspctl-image: image verification failed\n", stderr);
        return 78;
    }
    std::printf("generation=%s\ndigest=%s\n", manifest->generation.c_str(), manifest->rootfs_digest.c_str());
    return 0;
}
