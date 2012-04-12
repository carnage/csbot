from csbot.core import Plugin, PluginFeatures
from csbot.util import nick


class Auth(Plugin):
    features = PluginFeatures()

    @features.command('auth.check')
    def auth_check(self, event):
        event.protocol.identify(event.data[0])

    @features.hook('userIdentified')
    def userIdentified(self, event):
        if event.account:
            event.protocol.msg(
                    '#cs-york-dev',
                    '{e.user} is identified to account '
                    '{e.account}'.format(e=event))
        else:
            event.protocol.msg('#cs-york-dev',
                               '{e.user} is not identified'.format(e=event))
