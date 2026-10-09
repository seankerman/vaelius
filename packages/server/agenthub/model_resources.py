def release_models(registry, stores, embedder):
    """Release native ONNX resources before interpreter teardown on macOS.

    Registry/store references form a cycle. Without explicit finite-run cleanup,
    CoreML resources can be finalized after their native mutexes during shutdown.
    This is cleanup, not an alternative inference provider or forced-success exit.
    """
    for store in stores.values():
        store.semantic_embedder = None
    if registry is not None:registry._stores.clear()
    model = getattr(embedder, "model", embedder)
    if getattr(model, "_runtime", None) is not None:
        model._runtime = None
    import gc
    gc.collect()
