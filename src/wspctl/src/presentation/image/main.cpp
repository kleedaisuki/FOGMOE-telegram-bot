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
 * @note 镜像生成属于受控构建流水线；该工具只验证 rootfs、manifest 和内容摘要。
 *       Image creation belongs to a controlled build pipeline; this tool only verifies rootfs, manifest, and content digest.
 */
int main(const int argc, char *argv[])
{
    /** @brief 待验证或 seal 的 rootfs / Rootfs to verify or seal. */
    std::filesystem::path base_root;
    /** @brief 已发布 images 根（仅 verify） / Published images root (verify only). */
    std::filesystem::path images_root;
    /** @brief OCI manifest 摘要（仅 seal） / OCI manifest digest (seal only). */
    std::string source_oci_manifest_digest;
    /** @brief OCI 平台（仅 seal） / OCI platform (seal only). */
    std::string platform;
    /** @brief 是否选择 verify 模式 / Whether verify mode was selected. */
    bool verify = false;
    /** @brief 是否选择 seal 模式 / Whether seal mode was selected. */
    bool seal = false;
    /** @brief 是否选择 sealed-root inspect 模式 / Whether sealed-root inspect mode was selected. */
    bool inspect = false;
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
        else if (option == "--inspect")
        {
            inspect = value == "true";
        }
        else if (option == "--base-root")
        {
            base_root = value;
        }
        else if (option == "--images-root")
        {
            images_root = value;
        }
        else if (option == "--source-oci-manifest-digest")
        {
            source_oci_manifest_digest = value;
        }
        else if (option == "--platform")
        {
            platform = value;
        }
        else
        {
            std::fputs("wspctl-image: unknown option\n", stderr);
            return 64;
        }
        index += 2;
    }
    const unsigned int selected_modes =
        static_cast<unsigned int>(verify) +
        static_cast<unsigned int>(seal) +
        static_cast<unsigned int>(inspect);
    if (selected_modes != 1U)
    {
        std::fputs(
            "wspctl-image: select exactly one of --verify true, --inspect true, or --seal\n",
            stderr);
        return 64;
    }
    if (seal)
    {
        if (base_root.empty() || source_oci_manifest_digest.empty() ||
            platform.empty() || !images_root.empty())
        {
            std::fputs(
                "usage: wspctl-image --seal --base-root ABS "
                "--source-oci-manifest-digest sha256:HEX "
                "--platform linux/ARCH\n",
                stderr);
            return 64;
        }
        const auto manifest = wspctl::seal_image_root(
            base_root, platform, source_oci_manifest_digest);
        if (!manifest)
        {
            std::fprintf(
                stderr,
                "wspctl-image: image sealing failed: %s\n",
                manifest.error().message.c_str());
            return 78;
        }
        std::printf(
            "source_oci_manifest_digest=%s\nrootfs_digest=%s\n",
            manifest->source_oci_manifest_digest.c_str(),
            manifest->rootfs_digest.c_str());
        return 0;
    }
    if (inspect)
    {
        if (base_root.empty() || !images_root.empty() ||
            !source_oci_manifest_digest.empty() || !platform.empty())
        {
            std::fputs(
                "usage: wspctl-image --inspect true --base-root ABS\n",
                stderr);
            return 64;
        }
        const auto manifest = wspctl::load_image_manifest(base_root);
        if (!manifest)
        {
            std::fprintf(
                stderr,
                "wspctl-image: sealed image inspection failed: %s\n",
                manifest.error().message.c_str());
            return 78;
        }
        std::printf(
            "source_oci_manifest_digest=%s\nrootfs_digest=%s\n",
            manifest->source_oci_manifest_digest.c_str(),
            manifest->rootfs_digest.c_str());
        return 0;
    }
    if (!verify || base_root.empty() || images_root.empty() ||
        !source_oci_manifest_digest.empty() || !platform.empty())
    {
        std::fputs("usage: wspctl-image --verify true --base-root ABS --images-root ABS\n", stderr);
        return 64;
    }
    const auto manifest = wspctl::validate_image_root(base_root, images_root);
    if (!manifest)
    {
        std::fprintf(
            stderr,
            "wspctl-image: image verification failed: %s\n",
            manifest.error().message.c_str());
        return 78;
    }
    std::printf(
        "source_oci_manifest_digest=%s\nrootfs_digest=%s\n",
        manifest->source_oci_manifest_digest.c_str(),
        manifest->rootfs_digest.c_str());
    return 0;
}
