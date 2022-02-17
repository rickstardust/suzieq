from ipaddress import ip_network

import numpy as np
import pandas as pd
from ciscoconfparse import CiscoConfParse

from suzieq.engines.pandas.engineobj import SqPandasEngine
from suzieq.shared.confutils import (get_access_port_interfaces,
                                     get_trunk_port_interfaces)
from suzieq.shared.utils import build_query_str


class InterfacesObj(SqPandasEngine):
    '''Backend class to handle manipulating interfaces table with pandas'''

    @staticmethod
    def table_name():
        '''Table name'''
        return 'interfaces'

    def get(self, **kwargs):
        """Handling state outside of regular filters"""
        state = kwargs.pop('state', '')
        iftype = kwargs.pop('type', '')
        ifname = kwargs.get('ifname', '')
        vrf = kwargs.pop('vrf', '')
        master = kwargs.pop('master', [])
        columns = kwargs.get('columns', [])
        user_query = kwargs.pop('query_str', '')
        vlan = kwargs.pop('vlan', '')
        portmode = kwargs.pop('portmode', '')

        addnl_fields = kwargs.get('addnl_fields', [])

        if vrf:
            master.extend(vrf)

        fields = self.schema.get_display_fields(columns)
        # path passes additional fields
        for f in addnl_fields:
            if f not in fields:
                fields.append(f)

        if columns == ['*']:
            fields.remove('sqvers')

        drop_cols = []
        if not ifname and iftype and iftype != ["all"]:
            df = super().get(type=iftype, master=master, **kwargs)
        elif not ifname and iftype != ['all']:
            df = super().get(master=master, type=['!internal'], **kwargs)
        else:
            df = super().get(master=master, **kwargs)

        if df.empty:
            return df

        if portmode or 'portmode' in columns or '*' in columns:
            for x in ['ipAddressList', 'ip6AddressList']:
                if x in columns or '*' in columns:
                    continue
                drop_cols.append(x)
            df = self._add_portmode(df)

        if vlan or "vlanList" in columns or '*' in columns:
            if 'portmode' not in columns and '*' not in columns:
                for x in ['ipAddressList', 'ip6AddressList']:
                    if x in columns:
                        continue
                    drop_cols.append(x)
                df = self._add_portmode(df)
            df = self._add_vlanlist(df)

        if state or portmode:
            query_str = build_query_str([], self.schema, state=state,
                                        portmode=portmode)

            df = df.query(query_str)

        if vlan:
            # vlan needs to be looked at even in vlanList
            vlan = [int(x) for x in vlan]
            query_str = f' (vlan == {vlan} or vlanList == {vlan})'
            df_exp = df.explode('vlanList').query(query_str)
            df = df[df.namespace.isin(df_exp.namespace.unique()) &
                    df.hostname.isin(df_exp.hostname.unique()) &
                    df.ifname.isin(df_exp.ifname.unique())]

        if user_query:
            df = self._handle_user_query_str(df, user_query)

        if not (iftype or ifname) and 'type' in df.columns:
            return df.query('type != "internal"') \
                     .reset_index(drop=True)[fields]
        else:
            return df.reset_index(drop=True)[fields]

    # pylint: disable=arguments-differ
    def aver(self, what="", **kwargs) -> pd.DataFrame:
        """Assert that interfaces are in good state"""

        ignore_missing_peer = kwargs.pop('ignore_missing_peer', False)

        if what == "mtu-value":
            result_df = self._assert_mtu_value(**kwargs)
        else:
            result_df = self._assert_interfaces(ignore_missing_peer, **kwargs)
        return result_df

    def summarize(self, **kwargs) -> pd.DataFrame:
        """Summarize interface information"""
        self._init_summarize(**kwargs)
        if self.summary_df.empty:
            return self.summary_df

        # Loopback interfaces on Linux have "unknown" as state
        self.summary_df["state"] = self.summary_df['state'] \
                                       .map({"unknown": "up",
                                             "up": "up", "down": "down",
                                             "notConnected": "notConnected"})

        self._summarize_on_add_field = [
            ('deviceCnt', 'hostname', 'nunique'),
            ('interfaceCnt', 'ifname', 'count'),
        ]

        self._summarize_on_add_with_query = [
            ('devicesWithL2Cnt', 'master == "bridge"', 'hostname', 'nunique'),
            ('devicesWithVxlanCnt', 'type == "vxlan"', 'hostname'),
            ('ifDownCnt', 'state != "up" and adminState == "up"', 'ifname'),
            ('ifAdminDownCnt', 'adminState != "up"', 'ifname'),
            ('ifWithMultipleIPCnt', 'ipAddressList.str.len() > 1', 'ifname'),
        ]

        self._summarize_on_add_list_or_count = [
            ('uniqueMTUCnt', 'mtu'),
            ('uniqueIfTypesCnt', 'type'),
            ('speedCnt', 'speed'),
        ]

        self._summarize_on_add_stat = [
            ('ifChangesStat', 'type != "bond"', 'numChanges'),
        ]

        self._summarize_on_perdevice_stat = [
            ('ifPerDeviceStat', '', 'ifname', 'count')
        ]

        self._gen_summarize_data()

        # The rest of the summary generation is too specific to interfaces
        original_summary_df = self.summary_df
        self.summary_df = original_summary_df.explode(
            'ipAddressList').dropna(how='any')

        if not self.summary_df.empty:
            self.nsgrp = self.summary_df.groupby(by=["namespace"])
            self._add_field_to_summary(
                'ipAddressList', 'nunique', 'uniqueIPv4AddrCnt')
        else:
            self._add_constant_to_summary('uniqueIPv4AddrCnt', 0)
        self.summary_row_order.append('uniqueIPv4AddrCnt')

        self.summary_df = original_summary_df \
            .explode('ip6AddressList') \
            .dropna(how='any') \
            .query('~ip6AddressList.str.startswith("fe80:")')

        if not self.summary_df.empty:
            self.nsgrp = self.summary_df.groupby(by=["namespace"])
            self._add_field_to_summary(
                'ip6AddressList', 'nunique', 'uniqueIPv6AddrCnt')
        else:
            self._add_constant_to_summary('uniqueIPv6AddrCnt', 0)
        self.summary_row_order.append('uniqueIPv6AddrCnt')

        self._post_summarize(check_empty_col='interfaceCnt')
        return self.ns_df.convert_dtypes()

    def _assert_mtu_value(self, **kwargs) -> pd.DataFrame:
        """Workhorse routine to match MTU value"""

        columns = ["namespace", "hostname", "ifname", "state", "mtu",
                   "timestamp"]

        matchval = kwargs.pop('matchval', [])
        result = kwargs.pop('result', '')

        matchval = [int(x) for x in matchval]

        result_df = self.get(columns=columns, **kwargs) \
                        .query('ifname != "lo"')

        if not result_df.empty:
            result_df['result'] = result_df.apply(
                lambda x, matchval: 'pass' if x['mtu'] in matchval else 'fail',
                axis=1, args=(matchval,))

        if result == "fail":
            result_df = result_df.query('result == "fail"')
        elif result == "pass":
            result_df = result_df.query('result == "pass"')

        return result_df

    # pylint: disable=too-many-statements
    def _assert_interfaces(self, ignore_missing_peer: bool, **kwargs) -> pd.DataFrame:
        """Workhorse routine that validates MTU match for specified input"""
        columns = kwargs.pop('columns', [])
        result = kwargs.pop('result', 'all')
        state = kwargs.pop('state', '')
        iftype = kwargs.pop('type', [])

        def _check_field(x, fld1, fld2, reason):
            if x.skipIfCheck or x.indexPeer < 0:
                return []

            if x[fld1] == x[fld2]:
                return []
            return reason

        def _check_ipaddr(x, fld1, fld2, reason):
            # If we have no peer, don't check
            if x.skipIfCheck or x.indexPeer < 0:
                return []

            if len(x[fld1]) != len(x[fld2]):
                return reason

            if (len(x[fld1]) != 0):
                if (x[fld1][0].split('/')[1] == "32" or
                    (ip_network(x[fld1][0], strict=False) ==
                        ip_network(x[fld2][0], strict=False))):
                    return []
            else:
                return []

            return reason

        columns = ['*']

        if not state:
            state = 'up'

        if not iftype:
            iftype = ['ethernet', 'bond_slave', 'subinterface', 'vlan', 'bond']

        if_df = self.get(columns=columns, type=iftype, state=state, **kwargs)
        if if_df.empty:
            if result != 'pass':
                if_df['result'] = 'fail'
                if_df['assertReason'] = 'No data'

            return if_df

        if_df = if_df.drop(columns=['description', 'routeDistinguisher',
                                    'interfaceMac'], errors='ignore')
        # Map subinterface into parent interface
        if_df['pifname'] = if_df.apply(
            lambda x: x['ifname'].split('.')[0]
            if x.type in ['subinterface', 'vlan']
            else x['ifname'], axis=1)

        # Thanks for Junos, remove all the useless parent interfaces
        # if we have a .0 interface since thats the real deal
        del_iflist = if_df.apply(lambda x: x.pifname
                                 if x['ifname'].endswith('.0') else '',
                                 axis=1) \
            .unique().tolist()

        if_df['type'] = if_df.apply(lambda x: 'ethernet'
                                    if x['ifname'].endswith('.0')
                                    else x['type'], axis=1)

        if_df = if_df.query(f'~ifname.isin({del_iflist})').reset_index()
        if_df['ifname'] = if_df.apply(
            lambda x: x['ifname'] if not x['ifname'].endswith('.0')
            else x['pifname'], axis=1)

        lldpobj = self._get_table_sqobj('lldp')
        mlagobj = self._get_table_sqobj('mlag')

        # can't pass all kwargs, because lldp acceptable arguements are
        # different than interface
        namespace = kwargs.get('namespace', None)
        hostname = kwargs.get('hostname', None)
        lldp_df = lldpobj.get(namespace=namespace, hostname=hostname) \
                         .query('peerIfname != "-"')

        mlag_df = mlagobj.get(namespace=namespace, hostname=hostname)
        if not mlag_df.empty:
            mlag_peerlinks = set(mlag_df
                                 .groupby(by=['namespace', 'hostname',
                                              'peerLink'])
                                 .groups.keys())
        else:
            mlag_peerlinks = set()

        if 'vlanList' not in if_df.columns:
            if_df['vlanList'] = [[] for i in range(len(if_df))]

        if lldp_df.empty:
            if result != 'pass':
                if_df['assertReason'] = 'No LLDP peering info'
                if_df['result'] = 'fail'

            return if_df

        # Now create a single DF where you get the MTU for the lldp
        # combo of (namespace, hostname, ifname) and the MTU for
        # the combo of (namespace, peerHostname, peerIfname) and then
        # pare down the result to the rows where the two MTUs don't match
        idf = (
            pd.merge(
                if_df,
                lldp_df,
                left_on=["namespace", "hostname", "pifname"],
                right_on=['namespace', 'hostname', 'ifname'],
                how="outer",
            )
            .drop(columns=['ifname_y', 'timestamp_y'])
            .rename({'ifname_x': 'ifname', 'timestamp_x': 'timestamp',
                     'adminState_x': 'adminState',
                     'ipAddressList_x': 'ipAddressList',
                     'ip6AddressList_x': 'ip6AddressList',
                     'portmode_x': 'portmode'}, axis=1)
        )
        idf_nonsubif = idf.query('~type.isin(["subinterface", "vlan"])')
        idf_subif = idf.query('type.isin(["subinterface", "vlan"])')

        # Replace the bond_slave port interface with the bond interface

        idf_nonsubif = idf_nonsubif.merge(
            idf_nonsubif,
            left_on=["namespace", "peerHostname", "peerIfname"],
            right_on=['namespace', 'hostname', 'ifname'],
            how="outer", suffixes=["", "Peer"])

        idf_subif = idf_subif.merge(
            idf_subif,
            left_on=["namespace", "peerHostname", "peerIfname", 'vlan'],
            right_on=['namespace', 'hostname', 'pifname', 'vlan'],
            how="outer", suffixes=["", "Peer"])

        combined_df = pd.concat(
            [idf_subif, idf_nonsubif]).reset_index(drop=True)

        combined_df = combined_df \
            .drop(columns=["hostnamePeer", "pifnamePeer",
                           "mgmtIP", "description"]) \
            .dropna(subset=['hostname', 'ifname']) \
            .drop_duplicates(subset=['namespace', 'hostname', 'ifname'])

        if combined_df.empty:
            if result != 'pass':
                if_df['assertReason'] = 'No LLDP peering info'
                if_df['result'] = 'fail'

            return if_df

        combined_df = combined_df.fillna(
            {'mtuPeer': 0, 'speedPeer': 0, 'typePeer': '',
             'peerHostname': '', 'peerIfname': '', 'indexPeer': -1})
        for fld in ['ipAddressListPeer', 'ip6AddressListPeer', 'vlanListPeer']:
            combined_df[fld] = combined_df[fld] \
                .apply(lambda x: x if isinstance(x, np.ndarray) else [])

        combined_df['assertReason'] = combined_df.apply(
            lambda x: []
            if (x['adminState'] == 'down' or
                (x['adminState'] == "up" and x['state'] == "up"))
            else [x.reason or "Interface Down"], axis=1)

        known_hosts = set(combined_df.groupby(by=['namespace', 'hostname'])
                          .groups.keys())
        # Mark interfaces that can be skippedfrom checking because you cannot
        # find a peer
        combined_df['skipIfCheck'] = combined_df.apply(
            lambda x:
            (x.master == 'bridge') or (x.type in ['bond_slave', 'vlan']),
            axis=1)

        combined_df['indexPeer'] = combined_df.apply(
            lambda x, kh: x.indexPeer
            if (x.namespace, x.hostname) in kh else -2,
            args=(known_hosts,), axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: ['No Peer Found']
            if x.indexPeer == -1 and not x.skipIfCheck else [],
            axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: ['Unpolled Peer']
            if x.indexPeer == -2 and not x.skipIfCheck else [],
            axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: _check_field(x, 'mtu', 'mtuPeer', ['MTU mismatch']),
            axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: _check_field(
                x, 'speed', 'speedPeer', ['Speed mismatch']),
            axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: []
            if (x.indexPeer < 0 or
                ((x['type'] == x['typePeer']) or
                 (x['type'] == 'vlan' and x['typePeer'] == 'subinterface') or
                    (x['type'].startswith('ether') and
                     x['typePeer'].startswith('ether'))))
            else ['type mismatch'],
            axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x: [] if (x.indexPeer < 0 or
                             (x['portmode'] == x['portmodePeer']))
            else ['portMode Mismatch'], axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x:
            _check_ipaddr(x, 'ipAddressList', 'ipAddressListPeer',
                          ['IP address mismatch']), axis=1)

        # We ignore MLAG peerlinks mainly because of NXOS erroneous output.
        # NXOS displays the VLANs associated with an interface via show vlan
        # which is then further pruned out by vPC. This pruned out list needs
        # to be extracted from the vPC output and used for the peerlink
        # instead of the output of show vlan for that interface. Since most
        # platforms perform their own MLAG consistency checks, we can skip
        # doing VLAN consistency check on the peerlink.
        # TODO: A better checker for MLAG peerlinks if needed at a later time.

        combined_df['assertReason'] += combined_df.apply(
            lambda x: [] if (((x.portmode in ['access', 'trunk']) and
                              (x.indexPeer < 0 or
                               (x['vlan'] == x['vlanPeer']))) or
                             (x.portmode in ['routed', 'unknown']))
            else ['pvid Mismatch'], axis=1)

        combined_df['assertReason'] += combined_df.apply(
            lambda x, mlag_peerlinks: []
            if ((x.indexPeer > 0 and
                ((x.namespace, x.hostname, x.master) not in mlag_peerlinks) and
                 (set(x['vlanList']) == set(x['vlanListPeer']))) or
                ((x.indexPeer < 0) or
                ((x.namespace, x.hostname, x.master) in mlag_peerlinks)))
            else ['VLAN set mismatch'], args=(mlag_peerlinks,), axis=1)

        if ignore_missing_peer:
            combined_df['result'] = combined_df.apply(
                lambda x: 'fail'
                if (len(x.assertReason) and
                    (x.assertReason[0] != 'No Peer Found'))
                else 'pass', axis=1)
        else:
            combined_df['result'] = combined_df.apply(
                lambda x: 'fail' if (len(x.assertReason)) else 'pass', axis=1)

        if result == "fail":
            combined_df = combined_df.query('result == "fail"').reset_index()
        elif result == "pass":
            combined_df = combined_df.query('result == "pass"').reset_index()

        combined_df['assertReason'] = combined_df['assertReason'].apply(
            lambda x: x if len(x) else '-'
        )

        return combined_df[['namespace', 'hostname', 'ifname', 'state',
                            'peerHostname', 'peerIfname', 'result',
                            'assertReason', 'timestamp']]

    def _add_portmode(self, df: pd.DataFrame):
        """Add the switchport-mode i.e. acceess/trunk/routed'''

        :param df[pd.Dataframe]: The dataframe to add vlanList to
        :returns: original dataframe with portmode col added and filterd
        """

        if df.empty:
            return df

        conf_df = self._get_table_sqobj('devconfig') \
            .get(namespace=df.namespace.unique().tolist(),
                 hostname=df.hostname.unique().tolist())

        devdf = self._get_table_sqobj('device') \
            .get(namespace=df.namespace.unique().tolist(),
                 hostname=df.hostname.unique().tolist(),
                 columns=['namespace', 'hostname', 'os', 'status', 'vendor'])

        pm_df = pd.DataFrame({'namespace': [], 'hostname': [],
                              'ifname': [], 'portmode': []})

        pm_list = []
        for row in conf_df.itertuples():
            # Check what type of device this is
            # TBD: SONIC support
            if not devdf.empty:
                nos = devdf[(devdf.namespace == row.namespace) &
                            (devdf.hostname == row.hostname)]['os'].tolist()[0]
                if any(x in nos for x in ['junos', 'panos']):
                    syntax = 'junos'
                else:
                    # The way we pull out Cumulus Linux conf is also ios-like
                    syntax = 'ios'
            else:
                # Heuristics now
                if '\ninterfaces {\n' in row.config or \
                   'paloaltonetworks' in row.config:
                    syntax = 'junos'
                else:
                    syntax = 'ios'
            try:
                conf = CiscoConfParse(row.config.split('\n'), syntax=syntax)
            except Exception:  # pylint: disable=broad-except
                continue

            pm_dict = get_access_port_interfaces(conf, nos)
            pm_list.extend([{'namespace': row.namespace,
                             'hostname': row.hostname,
                             'ifname': k,
                             'portmode': 'access',
                             'vlan': v} for k, v in pm_dict.items()])
            pm_dict = get_trunk_port_interfaces(conf, nos)
            pm_list.extend([{'namespace': row.namespace,
                             'hostname': row.hostname,
                             'ifname': k,
                             'portmode': 'trunk',
                             'vlan': v} for k, v in pm_dict.items()])

        pm_df = pd.DataFrame(pm_list)

        if pm_df.empty:
            df['portmode'] = np.where(df.ipAddressList.str.len() == 0,
                                      'unknown',
                                      'routed')
            df['portmode'] = np.where(df.ip6AddressList.str.len() != 0,
                                      'routed', df.portmode)
            return df

        df = df.merge(pm_df, how='left', on=[
                      'namespace', 'hostname', 'ifname'],
                      suffixes=['', '_y']) \
            .fillna({'portmode': 'routed', 'vlan': 0})

        df.loc[df.ifname == "bridge", 'portmode'] = ''
        if 'vlan_y' in df.columns:
            df['vlan'] = np.where(df.vlan_y.isnull(), df.vlan,
                                  df.vlan_y)

        df['portmode'] = np.where(df.adminState != 'up', '',
                                  df.portmode)
        # handle EOS and other VXLAN ports which treat the interface
        # as a trunk port, as opposed to the access port mode of
        # the upto Cumulus 4.2.x Vxlan ports
        vxlan_ports = df.type == "vxlan"
        df.loc[vxlan_ports, 'portmode'] = df.loc[vxlan_ports] \
            .apply(lambda x: 'trunk' if x['portmode'] == 'routed'
                   else x['portmode'], axis=1)
        return df.drop(columns=['portmode_y', 'vlan_y'],
                       errors='ignore')

    def _add_vlanlist(self, df: pd.DataFrame):
        """Add list of active, unpruned VLANs on trunked ports

        :param df[pd.Dataframe]: The dataframe to add vlanList to
        :returns: original dataframe with vlanList col added
        """

        if df.empty:
            return df

        vlan_df = self._get_table_sqobj('vlan') \
                      .get(namespace=df.namespace.unique().tolist(),
                           hostname=df.hostname.unique().tolist())

        if vlan_df.empty:
            return df

        # Transform the list of VLANs from VLAN-oriented to interface oriented
        vlan_if_df = vlan_df.explode('interfaces') \
                            .groupby(by=['namespace', 'hostname',
                                         'interfaces'])['vlan'].unique() \
                            .reset_index() \
                            .rename(columns={'interfaces': 'ifname',
                                             'vlan': 'vlanList'})

        vlan_if_df['vlanList'] = vlan_if_df.vlanList.apply(sorted)

        df = df.merge(vlan_if_df, how='left',
                      on=['namespace', 'hostname', 'ifname'])

        isnull = df.vlanList.isnull()
        df.loc[isnull, 'vlanList'] = pd.Series([[]] * isnull.sum()).values

        # Now remove the vlanList from all the access and routed ports
        # We leave them on the unknown ports because we may not have gotten
        # the config data to identify the port mode
        not_trunk = df.portmode.isin(["access", "routed"])
        df.loc[not_trunk, 'vlanList'] = pd.Series(
            [[]] * not_trunk.sum()).values

        return df
