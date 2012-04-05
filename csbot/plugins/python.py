import os
from StringIO import StringIO

from pypy.translator.sandbox import pypy_interact

from csbot.core import Plugin, command


class PythonSandbox(Plugin):
    @command('>>>')
    def eval(self, event):
        r, w = os.pipe()
        code = 'import os; os.write({}, str(eval(os.read({}, 512))))'.format(r, w)
        sandbox = pypy_interact.PyPySandboxedProc(
                self.cfg('sandbox_bin'),
                [
                    '-c', code,
                ]
        )
        sandbox.settimeout(5)
        code_output = StringIO()
        code_log = StringIO()
        try:
            os.write(w, event.raw_data)
            sandbox.interact(stdin=StringIO(), stdout=code_output, stderr=code_log)
            print code_output.getvalue(), code_log.getvalue()

            reply = ''
            while True:
                chunk = os.read(r, 512)
                if chunk:
                    reply += chunk
                else:
                    break
            event.reply(reply)
        finally:
            sandbox.kill()
            os.close(r)
            os.close(w)
