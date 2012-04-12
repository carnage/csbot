from twisted.internet.defer import Deferred, inlineCallbacks

from csbot.core import Plugin, PluginFeatures
from csbot.util import nick


class Auth(Plugin):
    features = PluginFeatures()

    def setup(self):
        self.account_queries = dict()

    @features.command('auth.check')
    @inlineCallbacks
    def auth_check(self, event):
        account = yield self.get_user_account(event, event.data[0])

        if account:
            event.reply('{user} is identified to {account}'.format(
                user=event.data[0], account=account))
        else:
            event.reply('{user} is not identified'.format(
                user=event.data[0]))

    @features.hook('userIdentified')
    def userIdentified(self, event):
        """Fire any waiting deferreds when we find the account a user is
        identified to.
        """
        deferreds = self.account_queries.pop(event.user, list())
        for d in deferreds:
            d.callback(event.account)

    def get_user_account(self, event, user):
        """Get a :class:`Deferred` that will yield the account name of *user*,
        or None if *user* isn't identified or doesn't exist.
        """
        d = Deferred()
        if user in self.account_queries:
            # If there is an outstanding request, just add a "me too"
            self.account_queries[user].append(d)
        else:
            # If there is no outstanding request, actually make the request
            self.account_queries[user] = [d]
            event.protocol.identify(user)
        return d
