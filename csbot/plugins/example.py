from csbot.core import Plugin, command


class EmptyPlugin(Plugin):
    pass


class Example(Plugin):
    @command('test')
    def test_command(self, event):
        event.reply(('test invoked: {0.user}, {0.channel}, '
                     '{0.data}, {0.raw_data}').format(event))

    @command('cfg')
    def test_cfg(self, event):
        if len(event.data) == 0:
            event.error("You need to tell me what to look for!")
        else:
            try:
                event.reply("{} = {}".format(event.data[0],
                                             self.cfg(event.data[0])))
            except KeyError:
                event.error("I don't know a {}".format(event.data[0]))

    def privmsg(self, user, channel, msg):
        print ">>>", msg

    def action(self, user, channel, action):
        print "*", action
