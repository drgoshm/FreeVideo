"""Real completed video-model blocks; no tensor reads or device imports."""
import json


class PreparedBlocks:
    def __init__(self, total):
        if type(total) is not int or total < 1:
            raise ValueError('Model block total must be a positive integer')
        self.total, self.done = total, 0

    def report(self, **fields):
        print(json.dumps(dict(fields, event='prepared_blocks', blocks=self.done, total=self.total)), flush=True)

    def complete(self, **fields):
        # Called only after a block's existing preparation and upload submission.
        # A submission count does not claim synchronized GPU upload completion.
        if self.done == self.total:
            raise ValueError('All model blocks have already been prepared')
        self.done += 1
        self.report(**fields)
