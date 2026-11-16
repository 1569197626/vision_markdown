from dify_plugin import DifyPluginEnv, Plugin


if __name__ == "__main__":
    Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=600, MAX_WORKER=2)).run()
