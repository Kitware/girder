import { initConfigView } from './views/ConfigView';

export const initRoutes = (girder) => {
    const { router, events, utilities } = girder;

    const exposePluginConfig = utilities.PluginUtils.exposePluginConfig;

    exposePluginConfig('homepage', 'plugins/homepage/config');

    router.route('plugins/homepage/config', 'homepageConfig', function () {
        events.trigger('g:navigateTo', initConfigView(girder));
    });
};
