import logging

from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver

from .choices import LinkStatusChoices
from .models import Cable, CablePath, CableTermination, Device, PathEndpoint, PowerPanel, Rack, Location, VirtualChassis
from .models.cables import trace_paths
from .utils import create_cablepath, rebuild_paths


#
# Location/rack/device assignment
#

@receiver(post_save, sender=Location)
def handle_location_site_change(instance, created, **kwargs):
    """
    Update child objects if Site assignment has changed. We intentionally recurse through each child
    object instead of calling update() on the QuerySet to ensure the proper change records get created for each.
    """
    if not created:
        instance.get_descendants().update(site=instance.site)
        locations = instance.get_descendants(include_self=True).values_list('pk', flat=True)
        Rack.objects.filter(location__in=locations).update(site=instance.site)
        Device.objects.filter(location__in=locations).update(site=instance.site)
        PowerPanel.objects.filter(location__in=locations).update(site=instance.site)


@receiver(post_save, sender=Rack)
def handle_rack_site_change(instance, created, **kwargs):
    """
    Update child Devices if Site or Location assignment has changed.
    """
    if not created:
        Device.objects.filter(rack=instance).update(site=instance.site, location=instance.location)


#
# Virtual chassis
#

@receiver(post_save, sender=VirtualChassis)
def assign_virtualchassis_master(instance, created, **kwargs):
    """
    When a VirtualChassis is created, automatically assign its master device (if any) to the VC.
    """
    if created and instance.master:
        master = Device.objects.get(pk=instance.master.pk)
        master.virtual_chassis = instance
        master.vc_position = 1
        master.save()


@receiver(pre_delete, sender=VirtualChassis)
def clear_virtualchassis_members(instance, **kwargs):
    """
    When a VirtualChassis is deleted, nullify the vc_position and vc_priority fields of its prior members.
    """
    devices = Device.objects.filter(virtual_chassis=instance.pk)
    for device in devices:
        device.vc_position = None
        device.vc_priority = None
        device.save()


#
# Cables
#

@receiver(trace_paths, sender=Cable)
def update_connected_endpoints(instance, created, raw=False, **kwargs):
    """
    When a Cable is saved, check for and update its two connected endpoints
    """
    logger = logging.getLogger('netbox.dcim.cable')
    if raw:
        logger.debug(f"Skipping endpoint updates for imported cable {instance}")
        return

    # TODO: Update link peers

    # Create/update cable paths
    if created:
        _terms = {
            'A': [t.termination for t in instance.terminations.filter(cable_end='A')],
            'B': [t.termination for t in instance.terminations.filter(cable_end='B')],
        }
        for nodes in _terms.values():
            # Examine type of first termination to determine object type (all must be the same)
            if not nodes:
                continue
            if isinstance(nodes[0], PathEndpoint):
                create_cablepath(nodes)
            else:
                rebuild_paths(nodes)
    elif instance.status != instance._orig_status:
        # We currently don't support modifying either termination of an existing Cable. (This
        # may change in the future.) However, we do need to capture status changes and update
        # any CablePaths accordingly.
        if instance.status != LinkStatusChoices.STATUS_CONNECTED:
            CablePath.objects.filter(_nodes__contains=instance).update(is_active=False)
        else:
            rebuild_paths([instance])


# @receiver(post_save, sender=CableTermination)
# def cache_cable_on_endpoints(instance, created, raw=False, **kwargs):
#     if not raw:
#         model = instance.termination_type.model_class()
#         model.objects.filter(pk=instance.termination_id).update(cable=instance.cable)
#
#
# @receiver(post_delete, sender=CableTermination)
# def clear_cable_on_endpoints(instance, **kwargs):
#     model = instance.termination_type.model_class()
#     model.objects.filter(pk=instance.termination_id).update(cable=None)


@receiver(post_delete, sender=Cable)
def retrace_cable_paths(instance, **kwargs):
    """
    When a Cable is deleted, check for and update its connected endpoints
    """
    for cablepath in CablePath.objects.filter(_nodes__contains=instance):
        cablepath.retrace()


@receiver(post_delete, sender=CableTermination)
def nullify_connected_endpoints(instance, **kwargs):
    """
    Disassociate the Cable from the termination object.
    """
    model = instance.termination_type.model_class()
    model.objects.filter(pk=instance.termination_id).update(_link_peer_type=None, _link_peer_id=None)
