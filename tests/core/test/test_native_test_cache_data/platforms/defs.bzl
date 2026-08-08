def _execution_platform_impl(ctx):
    return [
        DefaultInfo(),
        ExecutionPlatformInfo(
            label = ctx.label.raw_target(),
            configuration = ctx.attrs.platform[PlatformInfo].configuration,
            executor_config = CommandExecutorConfig(
                allow_cache_uploads = ctx.attrs.allow_cache_uploads,
                local_enabled = True,
                remote_cache_enabled = ctx.attrs.remote_cache_enabled,
                remote_enabled = False,
                remote_execution_properties = {
                    "ISA": "x86_64",
                    "OSFamily": "linux",
                },
            ),
        ),
    ]

smoke_execution_platform = rule(
    impl = _execution_platform_impl,
    attrs = {
        "allow_cache_uploads": attrs.bool(),
        "platform": attrs.dep(providers = [PlatformInfo]),
        "remote_cache_enabled": attrs.bool(),
    },
)
