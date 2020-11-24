import time
from nubia import command, argument
import pandas as pd

from suzieq.cli.sqcmds.command import SqCommand
from suzieq.sqobjects.device import DeviceObj


@command("device", help="Act on device data")
class DeviceCmd(SqCommand):
    """device command"""

    def __init__(
            self,
            engine: str = "",
            hostname: str = "",
            start_time: str = "",
            end_time: str = "",
            view: str = "latest",
            namespace: str = "",
            format: str = "",
            columns: str = "default",
    ) -> None:
        super().__init__(
            engine=engine,
            hostname=hostname,
            start_time=start_time,
            end_time=end_time,
            view=view,
            namespace=namespace,
            columns=columns,
            format=format,
            sqobj=DeviceObj,
        )

    def _get(self):
        # Get the default display field names
        if self.columns != ["default"]:
            self.ctxt.sort_fields = None
        else:
            self.ctxt.sort_fields = []

        if 'uptime' in self.columns:
            self.columns = ['bootupTimestamp' if x == 'uptime' else x
                            for x in self.columns]
        df = self.sqobj.get(
            hostname=self.hostname, columns=self.columns,
            namespace=self.namespace,
        )
        # Convert the bootup timestamp into a time delta
        if not df.empty and 'bootupTimestamp' in df.columns:
            uptime_cols = (df['timestamp'] -
                           pd.to_datetime(df['bootupTimestamp']*1000,
                                          unit='ms', errors='ignore'))
            uptime_cols = pd.to_timedelta(uptime_cols, unit='ms')
            df.insert(len(df.columns)-1, 'uptime', uptime_cols)
            df = df.drop(columns=['bootupTimestamp'])

        return df

    @command("show", help="Show device information")
    def show(self):
        """
        Show device info
        """
        if self.columns is None:
            return

        now = time.time()
        df = self._get()
        self.ctxt.exec_time = "{:5.4f}s".format(time.time() - now)
        return self._gen_output(df)

    @command("top")
    @argument("what", description="Field you want to see top for",
              choices=["uptime"])
    @argument("count", description="How many top entries")
    @argument("reverse", description="True see Bottom n",
              choices=["True", "False"])
    def top(self, what: str = "flaps", count: int = 5, reverse: str = "False"):
        """
        Show top n entries based on specific field
        """

        # Device uptime is a field whose value is derived and calculated at
        # this level. So call get and then perform top on the data obtained

        now = time.time()
        if (self.columns != ['default'] and self.columns != ['*']
                and 'uptime' not in self.columns):
            self.columns.append('bootupTimestamp')
        df = self._get()
        if 'bootupTimestamp' in self.columns:
            self.columns.remove('bootupTimestamp')

        if not df.empty:
            if reverse == "True":
                topdf = df.nsmallest(count, columns='uptime', keep="all") \
                          .head(count)
            else:
                topdf = df.nlargest(count, columns='uptime', keep="all") \
                          .head(count)
        else:
            topdf = df

        self.ctxt.exec_time = "{:5.4f}s".format(time.time() - now)
        self._gen_output(topdf, sort=False)
