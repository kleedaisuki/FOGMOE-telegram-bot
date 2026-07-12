"""媒体上游与资源边界错误 / Media upstream and resource-boundary errors."""


class UpstreamUnavailable(RuntimeError):
    """可重试的上游媒体故障 / Retryable upstream-media failure."""


class ArtifactTooLarge(ValueError):
    """下载制品超过显式字节上限 / Downloaded artifact exceeds the explicit byte limit."""
