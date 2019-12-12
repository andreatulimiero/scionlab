# Copyright 2018 ETH Zurich
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ipaddress
from typing import List, Tuple, Set

from django import urls
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.html import format_html
from django.contrib.auth.models import User

import scionlab.tasks
from scionlab.models.core import (
    AS,
    Interface,
    Link,
    BorderRouter,
    Host
)
from scionlab.models.vpn import VPNClient
from scionlab.defines import (
    USER_AS_ID_BEGIN,
    USER_AS_ID_END,
)
from scionlab.util import as_ids

_MAX_LEN_CHOICES_DEFAULT = 16
_VAGRANT_VM_LOCAL_IP = '10.0.2.15'


class UserASManager(models.Manager):
    def create(self,
               owner: User,
               installation_type: str,
               isd: int,
               as_id: str = None,
               label: str = None) -> 'UserAS':
        """Create a UserAS

        :param User owner: owner of this UserAS
        :param str installation_type:
        :param int isd:
        :param str as_id: optional as_id, if None is given, the next free ID is chosen
        :param str label: optional label

        :returns: UserAS
        """
        owner.check_as_quota()
        assert isd, "No ISD provided"

        if as_id:
            as_id_int = as_ids.parse(as_id)
        else:
            as_id_int = self.get_next_id()
            as_id = as_ids.format(as_id_int)

        user_as = UserAS(
            owner=owner,
            label=label,
            isd=isd,
            as_id=as_id,
            as_id_int=as_id_int,
            installation_type=installation_type,
        )

        user_as.init_keys()
        user_as.generate_certificate_chain()
        user_as.save()
        user_as.init_default_services()

        return user_as

    def get_next_id(self):
        """
        Get the next available UserAS id.
        """
        max_id = self._max_id()
        if max_id is not None:
            if max_id >= USER_AS_ID_END:
                raise RuntimeError('UserAS-ID range exhausted')
            return max(USER_AS_ID_BEGIN, max_id + 1)
        else:
            return USER_AS_ID_BEGIN

    def _max_id(self):
        """
        :returns: the max `as_id_int` of all UserASes, or None
        """
        return self.aggregate(models.Max('as_id_int')).get('as_id_int__max', None)


class UserAS(AS):
    MAX_AP_PER_USERAS = 5
    VM = 'VM'
    PKG = 'PKG'
    SRC = 'SRC'
    INSTALLATION_TYPES = (
        (VM,  format_html('Run SCION in a <em>Vagrant</em> virtual machine '
                          '<i class="text-muted">(simplest approach)</i>')),
        (PKG, 'SCION installation from packages'),
        (SRC, format_html('SCION installation from sources '
                          '<i class="text-muted">(for developers)</i>')),
    )

    installation_type = models.CharField(
        choices=INSTALLATION_TYPES,
        max_length=_MAX_LEN_CHOICES_DEFAULT,
        default=VM
    )

    objects = UserASManager()

    class Meta:
        verbose_name = 'User AS'
        verbose_name_plural = 'User ASes'

    def get_absolute_url(self):
        return urls.reverse('user_as_detail', kwargs={'pk': self.pk})

    def update_attachments(self,
                           att_confs: List['AttachmentConf'] = [],
                           deleted_links: List[Link] = []):
        """
        Update the attachments of the UserAS, handling their creation, update and deletion.
        """
        # Attachment points of active connections
        aps_set = set(c.attachment_point for c in att_confs)
        # Attachment points of inactive connections
        aps_set |= set(l.interfaceA.AS.attachment_point_info for l in deleted_links)
        isds = set(c.attachment_point.AS.isd for c in att_confs if c.active is True)
        assert len(isds) <= 1, "ISD consistency infringed"
        # Update ISD if needed
        if len(isds) == 1:
            isd = isds.pop()
            if isd != self.isd:
                self._change_isd(isd)

        for att_conf in att_confs:
            # Update attachments
            self._create_or_update_attachment(att_conf)
        for deleted_link in deleted_links:
            # Delete links
            self._delete_attachment(deleted_link)
        for ap in aps_set:
            # Bump AttachmentPoints configurations
            ap.split_border_routers()
            ap.trigger_deployment()

        # Deactivate unused VPNClients
        active_vpns = set(c.attachment_point.vpn for c in att_confs if c.active and c.use_vpn)
        for vpn_client in VPNClient.objects.filter(host=self.host):
            if vpn_client.vpn not in active_vpns:
                vpn_client.active = False
                vpn_client.save()
        self.save()

    def _create_or_update_attachment(self, att_conf: 'AttachmentConf'):
        """
        Proxy function which calls create or update over a UserAS attachment point
        configuration, based on the existence of `att_conf.link`
        """
        if att_conf.link is None:
            assert att_conf.active, "Cannot deactivate a non existing Link"
            return self._create_attachment(att_conf)
        else:
            return self._update_attachment(att_conf)

    def _create_attachment(self, att_conf: 'AttachmentConf'):
        """
        Attach a UserAS to the specified attachment point creating the `Link` and the `Interface`s,
        and if needed a `VPNClient`. The new `Link` is stored in `att_conf.link`
        """
        ap = att_conf.attachment_point

        if att_conf.use_vpn:
            iface_client, iface_ap = self._create_or_update_vpn_connection(att_conf)
        else:
            if self.installation_type == UserAS.VM:
                att_conf.bind_ip = _VAGRANT_VM_LOCAL_IP

            iface_client = Interface.objects.create(
                border_router=BorderRouter.objects.first_or_create(self.host),
                public_ip=att_conf.public_ip,
                public_port=att_conf.public_port,
                bind_ip=att_conf.bind_ip,
                bind_port=att_conf.bind_port
            )
            iface_ap = Interface.objects.create(ap.get_border_router_for_useras_interface())

        link = Link.objects.create(
            type=Link.PROVIDER,
            interfaceA=iface_ap,
            interfaceB=iface_client
        )
        att_conf.link = link

    def _update_attachment(self, att_conf: 'AttachmentConf'):
        """
        Update the `Link` between the `UserAS` and the `AttachmentPoint`,
        and the respective `Interface`s (A and B)
        """
        ap = att_conf.attachment_point

        if att_conf.use_vpn:
            self._create_or_update_vpn_connection(att_conf)
        else:
            if self.installation_type == UserAS.VM:
                att_conf.bind_ip = _VAGRANT_VM_LOCAL_IP

            iface_ap, iface_client = att_conf.link.interfaceA, att_conf.link.interfaceB
            iface_client.update(public_ip=att_conf.public_ip,
                                public_port=att_conf.public_port,
                                bind_ip=att_conf.bind_ip,
                                bind_port=att_conf.bind_port)
            iface_ap.update(
                border_router=ap.get_border_router_for_useras_interface(),
                public_ip=None,
                public_port=None
            )

        att_conf.link.update(active=att_conf.active)
        att_conf.link.save()

    def _delete_attachment(self, deleted_link: List[Link]):
        deleted_link.delete()

    def _create_or_update_vpn_connection(self,
                                         att_conf: 'AttachmentConf') -> Tuple[Interface, Interface]:
        """
        Create or reuse a `VPNClient` to connect the `UserAS` to the `AttachmentPoint`.
        The function also takes care of the `AttachmentPoint`'s `Interface`.
        :returns: `Interface`s of the `UserAS` and the `AttachmentPoint`
        """
        assert att_conf.attachment_point.vpn, "VPN not supported by this attachment point"
        ap = att_conf.attachment_point
        host = self.hosts.get()
        # Reuse VPNClient if there's one with this AttachmentPoint
        vpn_client = host.vpn_clients.filter(vpn=ap.vpn).first()
        if not vpn_client:
            vpn_client = ap.vpn.create_client(host, active=True)
        elif not vpn_client.active:
            vpn_client.active = True
            vpn_client.save()

        if att_conf.link is not None:
            iface_ap, iface_client = att_conf.link.interfaceA, att_conf.link.interfaceB
            iface_client.update(public_ip=vpn_client.ip,
                                public_port=att_conf.public_port,
                                bind_ip=None,
                                bind_port=None)
            iface_ap.update(
                    border_router=ap.get_border_router_for_useras_interface(),
                    public_ip=att_conf.attachment_point.vpn.server_vpn_ip
            )
        else:
            border_router = BorderRouter.objects.first_or_create(host)
            iface_client = Interface.objects.create(border_router=border_router,
                                                    public_ip=vpn_client.ip,
                                                    public_port=att_conf.public_port)
            iface_ap = Interface.objects.create(
                border_router=att_conf.attachment_point.get_border_router_for_useras_interface(),
                public_ip=att_conf.attachment_point.vpn.server_vpn_ip)
        return iface_client, iface_ap

    def update(self,
               label,
               installation_type):
        """Update this UserAS instance and immediately `save`.

        Updates the related host, interface and link instances and will trigger
        a configuration bump for the hosts of the affected attachment point(s).
        """
        # XXX Useless assignment since the model instance's fields are already up to date
        self.label = label
        self.host.update()

        self.save()

    @property
    def host(self):
        return self.hosts.get()  # UserAS always has only one host

    @staticmethod
    def is_link_over_vpn(iface: Interface) -> bool:
        """
        Returns whether this UserAS interface corresponds to a link over VPN.
        """
        assert hasattr(iface.AS, 'useras')
        return iface.host.vpn_clients.filter(ip=iface.public_ip).exists()

    def is_active(self) -> bool:
        """
        Does the user have any active `Interface`?
        """
        return any(self.interfaces
                       .prefetch_related('link_as_interfaceB__active')
                       .values_list('link_as_interfaceB__active', flat=True)
                       .all())

    def update_active(self, active: bool):
        """
        Set the UserAS to be active/inactive by activating/deactivating all the links with
        attachment points. This will trigger a deployment of all the attachment points configuration
        """
        # FIXME(andrea_tulimiero): Update a compatible subset of attachment points
        for iface in self.interfaces.all():
            link = iface.link()
            link.update_active(active)
            link.interfaceA.host.AS.attachment_point_info.trigger_deployment()

    def attachment_points(self, active=True) -> List['AttachmentPoint']:
        """
        :returns: a list of attachments points to which the user is attached to
        """
        def _ap_of_iface(iface: Interface) -> AttachmentPoint:
            return iface.link_as_interfaceB.interfaceA.AS.attachment_point_info

        # Filter all interfaces all the current UserAS
        query = Interface.objects.filter(AS=self)
        # Select related to hit the databes less often
        query = query.select_related('link_as_interfaceB__interfaceA__AS__attachment_point_info')
        if active:
            query = query.filter(link_as_interfaceB__active=True)
        return [_ap_of_iface(iface) for iface in query]


class AttachmentPoint(models.Model):
    AS = models.OneToOneField(
        AS,
        related_name='attachment_point_info',
        on_delete=models.CASCADE,
    )
    vpn = models.OneToOneField(
        'VPN',
        null=True,
        blank=True,
        related_name='+',
        on_delete=models.SET_NULL
    )

    def __str__(self):
        return str(self.AS)

    def get_border_router_for_useras_interface(self) -> BorderRouter:
        """
        Selects the preferred border router on which the Interfaces to UserASes should be configured

        Note: the border router effectively used will be be overwritten by `split_border_routers`.

        :returns: a `BorderRouter` of the related `AS`
        """
        host = self._get_host_for_useras_attachment()
        return BorderRouter.objects.first_or_create(host)

    def check_vpn_available(self):
        """
        Raise ValidationError if the attachment point does not support VPN.
        """
        if self.vpn is None:
            raise ValidationError("Selected attachment point does not support VPN",
                                  code='attachment_point_no_vpn')

    def trigger_deployment(self):
        """
        Trigger the deployment for the attachment point configuration (after the current transaction
        is successfully committed).

        The deployment is rate limited, max rate controlled by
        settings.DEPLOYMENT_PERIOD.
        """
        for host in self.AS.hosts.iterator():
            transaction.on_commit(lambda: scionlab.tasks.deploy_host_config(host))

    def supported_ip_versions(self) -> Set[int]:
        """
        Returns the IP versions for the host where the user ASes will attach to
        """
        host = self._get_host_for_useras_attachment()
        return {ipaddress.ip_address(host.public_ip).version}

    def _get_host_for_useras_attachment(self) -> Host:
        """
        Finds the host where user ASes would attach to
        """
        if self.vpn is not None:
            assert (self.vpn.server.AS == self.AS)
            host = self.vpn.server
        else:
            host = self.AS.hosts.filter(public_ip__isnull=False)[0]
        return host

    def split_border_routers(self, max_ifaces=10):
        """
        This is a workaround for an (apparent) issue with the border router, that cannot handle more
        than ~12 interfaces per process; this problem seemed to be fixed but is apparently still
        here. This is a hopefully temporary patch.

        Will create / remove border routers so no one of them has more than
        the specified limit of interfaces. The links to parent ASes will
        always remain in a different border router.
        :param int max_ifaces The maximum number of interfaces per BR
        """
        host = self._get_host_for_useras_attachment()
        # find the *active* interfaces attaching for UserASes (attaching_ifaces) and the rest
        # (infra_ifaces)
        ifaces = host.interfaces.active().order_by('interface_id')
        attaching_ifaces = ifaces.filter(
            link_as_interfaceA__type=Link.PROVIDER,
            link_as_interfaceA__interfaceB__AS__owner__isnull=False)
        infra_ifaces = ifaces.exclude(pk__in=attaching_ifaces)

        # attaching non children all to one BR:
        infra_br = BorderRouter.objects.first_or_create(host)
        brs_to_delete = list(
            host.border_routers.order_by('pk').exclude(pk=infra_br.pk).values_list('pk', flat=True))
        brs_to_delete.reverse()
        infra_ifaces.update(border_router=infra_br)
        # attaching children to several BRs:
        attaching_ifaces = attaching_ifaces.all()
        for i in range(0, len(attaching_ifaces), max_ifaces):
            if brs_to_delete:
                br = BorderRouter.objects.get(pk=brs_to_delete.pop())
            else:
                br = BorderRouter.objects.create(host=host)
            for j in range(i, min(len(attaching_ifaces), i + max_ifaces)):
                iface = attaching_ifaces[j]
                iface.border_router = br
                iface.save()
            br.save()
        # squirrel away the *inactive* interfaces somewhere...
        host.interfaces.inactive().update(border_router=infra_br)

        # delete old BRs
        if brs_to_delete:
            BorderRouter.objects.filter(pk__in=brs_to_delete).delete()

    @staticmethod
    def from_link(link: Link) -> 'AttachmentPoint':
        """
        :returns: an attachment point given a link with a UserAS
        """
        assert hasattr(link.interfaceB.AS, 'useras')
        return link.interfaceA.AS.attachment_point_info


class AttachmentConf:
    """
    Helper class that reflects the state of a UserAS attachment configuration
    """

    def __init__(self, attachment_point,
                 public_ip, public_port, bind_ip, bind_port,
                 use_vpn, active=True, link=None):
        self.attachment_point = attachment_point
        self.public_ip = public_ip
        self.public_port = public_port
        self.bind_ip = bind_ip
        self.bind_port = bind_port
        self.use_vpn = use_vpn
        self.active = active
        self.link = link

    @staticmethod
    def attachment_points(att_confs: List['AttachmentConf']) -> List[AttachmentPoint]:
        """
        :returns List[AttachmentPoint]: the attachment points in a list of `AttachmentConf`s
        """
        return [att_conf.attachment_point for att_conf in att_confs]

    def __repr__(self) -> str:
        _str = 'AttachmentPointConf('
        for k, v in self.__dict__.items():
            _str += '{}={}, '.format(k, v)
        return _str[:-2] + ')'
