def _native_test_impl(ctx):
    return [
        DefaultInfo(),
        ExternalRunnerTestInfo(
            command = [ctx.attrs.python, ctx.attrs.script, ctx.attrs.resource],
            run_from_project_root = False,
            supports_test_execution_caching = ctx.attrs.supports_test_execution_caching,
            type = "lionhead",
            use_project_relative_paths = False,
        ),
    ]

native_test = rule(
    impl = _native_test_impl,
    attrs = {
        "python": attrs.string(),
        "resource": attrs.source(),
        "script": attrs.source(),
        "supports_test_execution_caching": attrs.bool(default = False),
    },
)

def _generated_native_test_impl(ctx):
    generated_script = ctx.actions.declare_output("generated_test.py")
    ctx.actions.run(
        [
            ctx.attrs.python,
            ctx.attrs.generator,
            ctx.attrs.producer_input,
            ctx.attrs.template,
            generated_script.as_output(),
        ],
        allow_cache_upload = False,
        category = "generate_test_binary",
    )
    return [
        DefaultInfo(generated_script),
        ExternalRunnerTestInfo(
            command = [ctx.attrs.python, generated_script, ctx.attrs.resource],
            run_from_project_root = False,
            supports_test_execution_caching = True,
            type = "lionhead",
            use_project_relative_paths = False,
        ),
    ]

generated_native_test = rule(
    impl = _generated_native_test_impl,
    attrs = {
        "generator": attrs.source(),
        "producer_input": attrs.source(),
        "python": attrs.string(),
        "resource": attrs.source(),
        "template": attrs.source(),
    },
)
