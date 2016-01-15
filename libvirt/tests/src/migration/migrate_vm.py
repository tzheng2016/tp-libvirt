import os
import re
import time
import signal
import logging
from subprocess import Popen, PIPE
from virttest import nfs, utils_libvirtd, utils_config, libvirt_vm
from virttest import libvirt_vm, remote, virsh, data_dir, utils_test
from virttest import virt_vm, utils_misc, utils_netperf, utils_selinux
from virttest.utils_test import libvirt
from autotest.client.shared import error, utils
from autotest.client import lv_utils
from virttest.libvirt_xml import vm_xml, capability_xml
from virttest.libvirt_xml.devices.sound import Sound
from virttest.libvirt_xml.devices.smartcard import Smartcard
from virttest.libvirt_xml.devices.watchdog import Watchdog
from virttest.staging import utils_memory
from virttest.utils_misc import SELinuxBoolean, get_cpu_vendor
from virttest.utils_net import IPv6Manager, \
    check_listening_port_remote_by_service, block_specific_ip_by_time
from virttest.utils_conn import SSHConnection, TCPConnection, \
    TLSConnection
from virttest.libvirt_xml.devices.disk import Disk
from autotest.client.shared import ssh_key


MIGRATE_RET = False


def migrate_vm(params):
    """
    Connect libvirt daemon
    """
    vm_name = params.get("main_vm", "")
    uri = params.get("desuri")
    options = params.get("virsh_options", "--live --verbose --unsafe")
    extra = params.get("extra_args", "")
    su_user = params.get("su_user", "")
    auth_user = params.get("server_user")
    auth_pwd = params.get("server_pwd")
    virsh_patterns = params.get("patterns_virsh_cmd", ".*100\s%.*")
    status_error = params.get("status_error", "no")
    timeout = int(params.get("migration_timeout", 30))
    for option in options.split():
        if option.startswith("--"):
            check_virsh_command_and_option("migrate", option)

    logging.info("Prepare migrate %s", vm_name)
    global MIGRATE_RET
    MIGRATE_RET = libvirt.do_migration(vm_name, uri, extra, auth_pwd,
                                       auth_user, options, virsh_patterns,
                                       su_user, timeout)

    if status_error == "no":
        if MIGRATE_RET:
            logging.info("Get an expected migration result.")
        else:
            raise error.TestFail("Can't get an expected migration result!!")
    else:
        if not MIGRATE_RET:
            logging.info("It's an expected error!!")
        else:
            raise error.TestFail("Unexpected return result!!")


def check_parameters(params):
    """
    Make sure all of parameters are assigned a valid value
    """
    client_ip = params.get("client_ip")
    server_ip = params.get("server_ip")
    ipv6_addr_src = params.get("ipv6_addr_src")
    ipv6_addr_des = params.get("ipv6_addr_des")
    client_cn = params.get("client_cn")
    server_cn = params.get("server_cn")
    client_ifname = params.get("client_ifname")
    server_ifname = params.get("server_ifname")

    args_list = [client_ip, server_ip, ipv6_addr_src,
                 ipv6_addr_des, client_cn, server_cn,
                 client_ifname, server_ifname]

    for arg in args_list:
        if arg and arg.count("ENTER.YOUR."):
            raise error.TestNAError("Please assign a value for %s!" % arg)


def config_libvirt(params):
    """
    Configure /etc/libvirt/libvirtd.conf
    """
    libvirtd = utils_libvirtd.Libvirtd()
    libvirtd_conf = utils_config.LibvirtdConfig()

    for k, v in params.items():
        libvirtd_conf[k] = v

    logging.debug("the libvirtd config file content is:\n %s" % libvirtd_conf)
    libvirtd.restart()

    return libvirtd_conf


def add_disk_xml(device_type, source_file,
                 image_size, policy,
                 disk_type="file"):
    """
    Create a disk xml file for attaching to a guest.

    :prams xml_file: path/file to save the disk XML
    :source_file: disk's source file
    :device_type: CD-ROM or floppy
    """
    if device_type != 'cdrom' or device_type != 'floppy':
        error.TestNAError("Only support 'cdrom' and 'floppy'"
                          " device type: %s" % device_type)

    dev_dict = {'cdrom': {'bus': 'ide', 'dev': 'hdc'},
                'floppy': {'bus': 'fdc', 'dev': 'fda'}}
    if image_size:
        cmd = "qemu-img create %s %s" % (source_file, image_size)
        logging.info("Prepare to run %s", cmd)
        utils.run(cmd)
    disk_class = vm_xml.VMXML.get_device_class('disk')
    disk = disk_class(type_name=disk_type)
    disk.device = device_type
    if device_type == 'cdrom':
        disk.driver = dict(name='qemu')
    else:
        disk.driver = dict(name='qemu', cache='none')

    disk_attrs_dict = {}
    if disk_type == "file":
        disk_attrs_dict['file'] = source_file

    if disk_type == "block":
        disk_attrs_dict['dev'] = source_file

    if policy:
        disk_attrs_dict['startupPolicy'] = policy

    logging.debug("The disk attributes dictionary: %s", disk_attrs_dict)
    disk.source = disk.new_disk_source(attrs=disk_attrs_dict)
    disk.target = dev_dict.get(device_type)
    disk.xmltreefile.write()
    logging.debug("The disk XML: %s", disk.xmltreefile)

    return disk.xml


def prepare_gluster_disk(params):
    """
    Setup glusterfs and prepare disk image.
    """
    gluster_disk = "yes" == params.get("gluster_disk")
    disk_format = params.get("disk_format", "qcow2")
    vol_name = params.get("vol_name")
    disk_img = params.get("disk_img")
    default_pool = params.get("default_pool", "")
    pool_name = params.get("pool_name")
    data_path = data_dir.get_data_dir()
    brick_path = params.get("brick_path")
    # Get the image path and name from parameters
    image_name = params.get("image_name")
    image_format = params.get("image_format")
    image_source = os.path.join(data_path,
                                image_name + '.' + image_format)

    # Setup gluster.
    host_ip = libvirt.setup_or_cleanup_gluster(True, vol_name,
                                               brick_path, pool_name)
    logging.debug("host ip: %s ", host_ip)
    image_info = utils_misc.get_image_info(image_source)
    if image_info["format"] == disk_format:
        disk_cmd = ("cp -f %s /mnt/%s" % (image_source, disk_img))
    else:
        # Convert the disk format
        disk_cmd = ("qemu-img convert -f %s -O %s %s /mnt/%s" %
                    (image_info["format"], disk_format, image_source, disk_img))

    # Mount the gluster disk and create the image.
    utils.run("mount -t glusterfs %s:%s /mnt; %s; umount /mnt"
              % (host_ip, vol_name, disk_cmd))

    return host_ip


def build_disk_xml(vm_name, disk_format, host_ip, disk_src_protocol,
                   vol_name_or_iscsi_target, disk_img=None, transport=None):
    """
    Try to rebuild disk xml
    """
    # Delete existed disks first.
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    disks_dev = vmxml.get_devices(device_type="disk")
    for disk in disks_dev:
        vmxml.del_device(disk)

    disk_xml = Disk(type_name="network")
    driver_dict = {"name": "qemu",
                   "type": disk_format,
                   "cache": "none"}
    disk_xml.driver = driver_dict
    disk_xml.target = {"dev": "vda", "bus": "virtio"}
    if disk_src_protocol == "gluster":
        disk_xml.device = "disk"
        vol_name = vol_name_or_iscsi_target
        source_dict = {"protocol": disk_src_protocol,
                       "name": "%s/%s" % (vol_name, disk_img)}
        host_dict = {"name": host_ip, "port": "24007"}
        if transport:
            host_dict.update({"transport": transport})
        disk_xml.source = disk_xml.new_disk_source(
            **{"attrs": source_dict, "hosts": [host_dict]})
    if disk_src_protocol == "iscsi":
        iscsi_target = vol_name_or_iscsi_target[0]
        lun_num = vol_name_or_iscsi_target[1]
        source_dict = {'protocol': disk_src_protocol,
                       'name': iscsi_target + "/" + str(lun_num)}
        host_dict = {"name": host_ip, "port": "3260"}
        if transport:
            host_dict.update({"transport": transport})
        disk_xml.source = disk_xml.new_disk_source(
            **{"attrs": source_dict, "hosts": [host_dict]})

    # Add the new disk xml.
    vmxml.add_device(disk_xml)
    vmxml.sync()


def get_cpu_xml_from_virsh_caps(runner=None):
    """
    Get CPU XML from virsh capabilities output
    """
    cmd = "virsh capabilities | awk '/<cpu>/,/<\/cpu>/'"
    out = ""
    if not runner:
        out = utils.system_output(cmd)
    else:
        out = runner(cmd)

    if not re.search('cpu', out):
        raise error.TestFail("Failed to get cpu XML: %s" % out)

    return out


def compute_cpu_baseline(cpu_xml, status_error="no"):
    """
    Compute CPU baseline
    """
    result = virsh.cpu_baseline(cpu_xml, ignore_status=True, debug=True)
    status = result.exit_status
    output = result.stdout.strip()
    err = result.stderr.strip()
    if status_error == "no":
        if status:
            raise error.TestFail("Failed to compute baseline CPU: %s" % err)
        else:
            logging.info("Succeed to compute baseline CPU: %s", output)
    else:
        if status:
            logging.info("It's an expected %s", err)
        else:
            raise error.TestFail("Unexpected return result: %s" % output)

    return output


def custom_cpu(vm_name, cpu_model, cpu_vendor, cpu_model_fallback="allow",
               cpu_feature_dict={}, cpu_mode="custom", cpu_match="exact"):
    """
    Custom guest cpu match/model/features, etc .
    """
    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    cpu_xml = vm_xml.VMCPUXML()
    cpu_xml.mode = cpu_mode
    cpu_xml.match = cpu_match
    cpu_xml.model = cpu_model
    cpu_xml.vendor = cpu_vendor
    cpu_xml.fallback = cpu_model_fallback
    if cpu_feature_dict:
        for k, v in cpu_feature_dict.items():
            cpu_xml.add_feature(k, v)
    vmxml['cpu'] = cpu_xml
    vmxml.sync()


def delete_video_device(vm_name):
    """
    Remove video device
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    logging.debug("The old VM XML:\n%s" % vmxml.xmltreefile)
    videos = vmxml.get_devices(device_type="video")
    for video in videos:
        vmxml.del_device(video)
    graphics = vmxml.get_devices(device_type="graphics")
    for graphic in graphics:
        vmxml.del_device(graphic)
    vmxml.sync()
    vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
    logging.debug("The VM XML after deleting video device: \n%s", vm_xml_cxt)


def update_sound_device(vm_name, sound_model):
    """
    Update sound device model
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    logging.debug("The old VM XML:\n%s" % vmxml.xmltreefile)
    sounds = vmxml.get_devices(device_type="sound")
    for sound in sounds:
        vmxml.del_device(sound)
    new_sound = Sound()
    new_sound.model_type = sound_model
    vmxml.add_device(new_sound)
    logging.debug("The VM XML with new sound model:\n%s" % vmxml.xmltreefile)
    vmxml.sync()


def add_watchdog_device(vm_name, watchdog_model, watchdog_action="none"):
    """
    Update sound device model
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    logging.debug("The old VM XML:\n%s" % vmxml.xmltreefile)
    watchdogs = vmxml.get_devices(device_type="watchdog")
    for watchdog in watchdogs:
        vmxml.del_device(watchdog)
    new_watchdog = Watchdog()
    new_watchdog.model_type = watchdog_model
    new_watchdog.action = watchdog_action
    vmxml.add_device(new_watchdog)
    logging.debug("The VM XML with new watchdog model:\n%s" % vmxml.xmltreefile)
    vmxml.sync()


def prepare_guest_watchdog(vm_name, vm, watchdog_model, watchdog_action="none",
                           mod_args="", watchdog_on=1, prepare_xml=True):
    """
    Prepare qemu guest agent on the VM.

    :param prepare_xml: Whether change VM's XML
    :param channel: Whether add agent channel in VM. Only valid if
                    prepare_xml is True
    :param start: Whether install and start the qemu-ga service
    """
    if prepare_xml:
        add_watchdog_device(vm_name, watchdog_model, watchdog_action)

    if not vm.is_alive():
        vm.start()

    session = vm.wait_for_login()

    def _has_watchdog_driver(watchdog_model):
        cmd = "lsmod | grep %s" % watchdog_model
        logging.debug("Run '%s' in VM", cmd)
        return session.cmd_status(cmd)

    def _load_watchdog_driver(watchdog_model, mod_args):
        if watchdog_model == "ib700":
            watchdog_model += "wdt"
        cmd = "modprobe %s %s" % (watchdog_model, mod_args)
        logging.debug("Run '%s' in VM", cmd)
        return session.cmd_status(cmd)

    def _remove_watchdog_driver(watchdog_model):
        if watchdog_model == "ib700":
            watchdog_model += "wdt"
        cmd = "modprobe -r %s" % watchdog_model
        logging.debug("Run '%s' in VM", cmd)
        return session.cmd_status(cmd)

    def _config_watchdog(default):
        cmd = "echo %s > /dev/watchdog" % default
        logging.debug("Run '%s' in VM", cmd)
        return session.cmd_status(cmd)

    try:
        if _has_watchdog_driver(watchdog_model):
            logging.info("Loading watchdog driver")
            _load_watchdog_driver(watchdog_model, mod_args)
            if _has_watchdog_driver(watchdog_model):
                raise virt_vm.VMError("Can't load watchdog driver in VM!")

        if mod_args != "":
            logging.info("Reconfigure %s with %s parameters", watchdog_model,
                         mod_args)
            _remove_watchdog_driver(watchdog_model)
            _load_watchdog_driver(watchdog_model, mod_args)
            if _has_watchdog_driver(watchdog_model):
                raise virt_vm.VMError("Can't load watchdog driver in VM!")

        logging.info("Turn watchdog on.")
        _config_watchdog(watchdog_on)
    finally:
        session.close()


def add_smartcard_device(vm_name, smartcard_type, smartcard_mode):
    """
    Update sound device model
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    logging.debug("The old VM XML:\n%s" % vmxml.xmltreefile)
    smartcards = vmxml.get_devices(device_type="smartcard")
    for smartcard in smartcards:
        vmxml.del_device(smartcard)
    new_smartcard = Smartcard()
    new_smartcard.smartcard_type = smartcard_type
    new_smartcard.smartcard_mode = smartcard_mode
    vmxml.add_device(new_smartcard)
    logging.debug("The VM XML with new sound model:\n%s" % vmxml.xmltreefile)
    vmxml.sync()


def update_disk_driver(vm_name, disk_name, disk_type, disk_cache):
    """
    Update disk driver
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    devices = vmxml.devices
    disk_index = devices.index(devices.by_device_tag('disk')[0])
    disks = devices[disk_index]
    disk_driver = disks.get_driver()
    if disk_name:
        disk_driver["name"] = disk_name
    if disk_type:
        disk_driver["type"] = disk_type
    if disk_cache:
        disk_driver["cache"] = disk_cache
    disks.set_driver(disk_driver)
    # SYNC VM XML change
    vmxml.devices = devices
    logging.debug("The VM XML with disk driver change:\n%s", vmxml.xmltreefile)
    vmxml.sync()


def update_interface_xml(vm_name, iface_address, iface_model=None,
                         iface_type=None):
    """
    Modify interface xml options
    """
    vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
    xml_devices = vmxml.devices
    iface_index = xml_devices.index(
        xml_devices.by_device_tag("interface")[0])
    iface = xml_devices[iface_index]

    if iface_model:
        iface.model = iface_model

    if iface_type:
        iface.type_name = iface_type

    if iface_address:
        addr_dict = {}
        if iface_address:
            for addr_option in iface_address.split(','):
                if addr_option != "":
                    d = addr_option.split('=')
                    addr_dict.update({d[0].strip(): d[1].strip()})
        if addr_dict:
            iface.address = iface.new_iface_address(
                **{"attrs": addr_dict})

    vmxml.devices = xml_devices
    vmxml.xmltreefile.write()
    vmxml.sync()


def check_virsh_command_and_option(command, option=None):
    """
    Check if virsh command exists
    """
    msg = "This version of libvirt does not support "
    if not virsh.has_help_command(command):
        raise error.TestNAError(msg + "virsh command '%s'" % command)

    if option and not virsh.has_command_help_match(command, option):
        raise error.TestNAError(msg + "virsh command '%s' with option '%s'"
                                % (command, option))


def run_remote_cmd(command, server_ip, server_user, server_pwd,
                   ret_status_output=True, ret_session_status_output=False,
                   timeout=60, client="ssh", port="22", prompt=r"[\#\$]\s*$"):
    """
    Run command on remote host
    """
    logging.info("Execute %s on %s", command, server_ip)
    session = remote.wait_for_login(client, server_ip, port,
                                    server_user, server_pwd,
                                    prompt)
    status, output = session.cmd_status_output(command, timeout)

    if ret_status_output:
        session.close()
        return (status, output)

    if ret_session_status_output:
        return (session, status, output)


def setup_netsever_and_launch_netperf(params):
    """
    Setup netserver and run netperf client
    """
    server_ip = params.get("server_ip")
    server_user = params.get("server_user")
    server_pwd = params.get("server_pwd")
    client_ip = params.get("client_ip")
    client_user = params.get("client_user")
    client_pwd = params.get("client_pwd")
    netperf_source = params.get("netperf_source")
    netperf_source = os.path.join(data_dir.get_root_dir(), netperf_source)
    client_md5sum = params.get("client_md5sum")
    client_path = params.get("client_path", "/var/tmp")
    server_md5sum = params.get("server_md5sum")
    server_path = params.get("server_path", "/var/tmp")
    compile_option_client = params.get("compile_option_client", "")
    compile_option_server = params.get("compile_option_server", "")
    # Run netperf with message size defined in range.
    netperf_test_duration = int(params.get("netperf_test_duration", 60))
    netperf_para_sess = params.get("netperf_para_sessions", "1")
    test_protocol = params.get("test_protocols", "TCP_STREAM")
    netperf_cmd_prefix = params.get("netperf_cmd_prefix", "")
    netperf_output_unit = params.get("netperf_output_unit", " ")
    netperf_package_sizes = params.get("netperf_package_sizes")
    test_option = params.get("test_option", "")
    direction = params.get("direction", "remote")

    n_client = utils_netperf.NetperfClient(client_ip,
                                           client_path,
                                           client_md5sum,
                                           netperf_source,
                                           client="ssh",
                                           port="22",
                                           username=client_user,
                                           password=client_pwd,
                                           compile_option=compile_option_client)

    logging.info("Start netserver on %s", server_ip)
    n_server = utils_netperf.NetperfServer(server_ip,
                                           server_path,
                                           server_md5sum,
                                           netperf_source,
                                           client="ssh",
                                           port="22",
                                           username=server_user,
                                           password=server_pwd,
                                           compile_option=compile_option_server)

    n_server.start()

    test_option += " -l %s" % netperf_test_duration
    start_time = time.time()
    stop_time = start_time + netperf_test_duration
    t_option = "%s -t %s" % (test_option, test_protocol)
    logging.info("Start netperf on %s", client_ip)
    n_client.bg_start(server_ip, t_option,
                      netperf_para_sess, netperf_cmd_prefix,
                      package_sizes=netperf_package_sizes)
    if utils_misc.wait_for(n_client.is_netperf_running, 10, 0, 1,
                           "Wait netperf test start"):
        logging.info("Start netperf on %s successfully.", client_ip)
        return (True, n_client, n_server)
    else:
        return (False, n_client, n_server)


def cleanup(objs_list):
    """
    Clean up test environment
    """
    # recovery test environment
    for obj in objs_list:
        obj.auto_recover = True
        del obj


def run(test, params, env):
    """
    Test remote access with TCP, TLS connection
    """
    test_dict = dict(params)
    vm_name = test_dict.get("main_vm")
    vm = env.get_vm(vm_name)
    start_vm = test_dict.get("start_vm", "no")
    status_error = test_dict.get("status_error", "no")
    transport = test_dict.get("transport")
    plus = test_dict.get("conn_plus", "+")
    config_ipv6 = test_dict.get("config_ipv6", "no")
    listen_addr = test_dict.get("listen_addr", "0.0.0.0")
    uri_port = test_dict.get("uri_port", ":22")
    server_ip = test_dict.get("server_ip")
    server_user = test_dict.get("server_user")
    server_pwd = test_dict.get("server_pwd")
    client_ip = test_dict.get("client_ip")
    client_user = test_dict.get("client_user")
    client_pwd = test_dict.get("client_pwd")
    server_cn = test_dict.get("server_cn")
    ipv6_addr_des = test_dict.get("ipv6_addr_des")
    portal_ip = test_dict.get("portal_ip", "127.0.0.1")
    restart_libvirtd = test_dict.get("restart_src_libvirtd", "no")
    driver = test_dict.get("test_driver", "qemu")
    uri_path = test_dict.get("uri_path", "/system")
    nfs_mount_dir = test_dict.get("nfs_mount_dir", "/var/lib/libvirt/migrate")
    setup_ssh = test_dict.get("setup_ssh", "yes")
    setup_tcp = test_dict.get("setup_tcp", "yes")
    setup_tls = test_dict.get("setup_tls", "yes")
    ssh_recovery = test_dict.get("ssh_auto_recovery", "yes")
    tcp_recovery = test_dict.get("tcp_auto_recovery", "yes")
    tls_recovery = test_dict.get("tls_auto_recovery", "yes")
    source_type = test_dict.get("vm_disk_source_type", "file")
    target_ip = test_dict.get("target_ip", "")
    adduser_cmd = test_dict.get("adduser_cmd")
    deluser_cmd = test_dict.get("deluser_cmd")
    host_uuid = test_dict.get("host_uuid")
    pause_vm = "yes" == test_dict.get("pause_vm", "no")
    reboot_vm = "yes" == test_dict.get("reboot_vm", "no")
    abort_job = "yes" == test_dict.get("abort_job", "no")
    ctrl_c = "yes" == test_dict.get("ctrl_c", "no")
    remote_path = test_dict.get("remote_libvirtd_conf",
                                "/etc/libvirt/libvirtd.conf")
    log_file = test_dict.get("libvirt_log", "/var/log/libvirt/libvirtd.log")
    run_migr_back = "yes" == test_dict.get(
        "run_migrate_cmd_in_back", "no")
    run_migr_front = "yes" == test_dict.get(
        "run_migrate_cmd_in_front", "yes")
    stop_libvirtd_remotely = "yes" == test_dict.get(
        "stop_libvirtd_remotely", "no")
    restart_libvirtd_remotely = "yes" == test_dict.get(
        "restart_libvirtd_remotely", "no")
    cdrom_image_size = test_dict.get("cdrom_image_size")
    cdrom_device_type = test_dict.get("cdrom_device_type")
    floppy_image_size = test_dict.get("floppy_image_size")
    floppy_device_type = test_dict.get("floppy_device_type")
    policy = test_dict.get("startup_policy", "")
    local_image = test_dict.get("local_disk_image")
    target_disk_image = test_dict.get("target_disk_image")
    target_dev = test_dict.get("target_dev", "")
    update_disk_source = "yes" == test_dict.get("update_disk_source", "no")

    mb_enable = "yes" == test_dict.get("mb_enable", "no")
    config_remote_hugepages = "yes" == test_dict.get("config_remote_hugepages",
                                                     "no")
    remote_tgt_hugepages = test_dict.get("remote_target_hugepages")
    remote_hugetlbfs_path = test_dict.get("remote_hugetlbfs_path")
    delay = int(params.get("delay_time", 10))

    stop_remote_guest = "yes" == test_dict.get("stop_remote_guest", "yes")
    memtune_options = test_dict.get("memtune_options")
    setup_nfs = "yes" == test_dict.get("setup_nfs", "yes")
    enable_virt_use_nfs = "yes" == test_dict.get("enable_virt_use_nfs", "yes")

    check_domain_state = "yes" == test_dict.get("check_domain_state", "no")
    expected_domain_state = test_dict.get("expected_domain_state")

    check_job_info = "yes" == test_dict.get("check_job_info", "yes")
    check_complete_job = test_dict.get("check_complete_job", "no")
    block_ip_addr = test_dict.get("block_ip_addr")
    block_time = test_dict.get("block_time")
    restart_vm = "yes" == test_dict.get("restart_vm", "no")
    diff_cpu_vendor = "yes" == test_dict.get("diff_cpu_vendor", "no")

    nbd_port = test_dict.get("nbd_port")
    target_image_size = test_dict.get("target_image_size")
    target_image_format = test_dict.get("target_image_format")
    create_target_image = "yes" == test_dict.get("create_target_image", "no")
    create_disk_src_backing_file = test_dict.get(
        "create_local_disk_backfile_cmd")
    create_disk_tgt_backing_file = test_dict.get(
        "create_remote_disk_backfile_cmd")

    log_level = test_dict.get("log_level", "1")
    log_filters = test_dict.get("log_filters",
                                '"1:json 1:libvirt 1:qemu 1:monitor 3:remote 4:event"')

    libvirtd_conf_dict = {"log_level": log_level,
                          "log_filters": log_filters,
                          "log_outputs": '"%s:file:%s"' % (log_level, log_file)}

    remote_port = test_dict.get("open_remote_listening_port")

    vol_name = test_dict.get("vol_name")
    brick_path = test_dict.get("brick_path")
    disk_src_protocol = params.get("disk_source_protocol")
    gluster_transport = test_dict.get("gluster_transport")
    iscsi_transport = test_dict.get("iscsi_transport")
    config_libvirtd = test_dict.get("config_libvirtd", "no")

    cpu_set = "yes" == test_dict.get("cpu_set", "no")
    vcpu_num = test_dict.get("vcpu_num", "1")
    vcpu_cpuset = test_dict.get("vcpu_cpuset")

    stress_type = test_dict.get("stress_type")
    stress_args = test_dict.get("stress_args")

    no_swap = "yes" == test_dict.get("no_swap", "no")
    down_time = test_dict.get("max_down_time")

    get_migr_cache = "yes" == test_dict.get("get_migrate_compcache", "no")
    set_migr_cache_size = test_dict.get("set_migrate_compcache_size")

    sound_model = test_dict.get("sound_model")
    source_file = test_dict.get("disk_source_file")

    # Process blkdeviotune parameters
    total_bytes_sec = test_dict.get("blkdevio_total_bytes_sec")
    read_bytes_sec = test_dict.get("blkdevio_read_bytes_sec")
    write_bytes_sec = test_dict.get("blkdevio_write_bytes_sec")
    total_iops_sec = test_dict.get("blkdevio_total_iops_sec")
    read_iops_sec = test_dict.get("blkdevio_read_iops_sec")
    write_iops_sec = test_dict.get("blkdevio_write_iops_sec")
    blkdevio_dev = test_dict.get("blkdevio_device")
    blkdevio_options = test_dict.get("blkdevio_options")

    tc_cmd = test_dict.get("tc_cmd")

    ssh_key.setup_ssh_key(server_ip, server_user, server_pwd, 22)

    migr_vm_back = "yes" == test_dict.get("migrate_vm_back", "no")
    if migr_vm_back:
        ssh_key.setup_remote_ssh_key(server_ip, server_user, server_pwd)

    # It's used to clean up SSH, TLS and TCP objs later
    objs_list = []

    # Default don't attach disk/cdrom to the guest.
    attach_disk = False

    # Make sure all of parameters are assigned a valid value
    check_parameters(test_dict)

    if vm.is_alive() and start_vm == "no":
        vm.destroy(gracefully=False)

    # Back up xml file.
    vmxml_backup = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)

    # Get current VM's memory
    current_mem = vmxml_backup.current_mem
    logging.debug("Current VM memory: %s", current_mem)

    # Disk XML file
    disk_xml = None

    # Add device type into a list
    dev_type_list = []

    if cdrom_device_type:
        dev_type_list.append(cdrom_device_type)

    if floppy_device_type:
        dev_type_list.append(floppy_device_type)

    # Add created image file into a list
    local_image_list = []
    remote_image_list = []

    # Defaut don't add new iptables rules
    add_iptables_rules = False

    # Converting time to second
    power = {'hours': 60 * 60, "minutes": 60, "seconds": 1}

    # Mounted hugepage filesystem
    HUGETLBFS_MOUNT = False

    os_ver_from = test_dict.get("os_ver_from")
    os_ver_to = test_dict.get("os_ver_to")
    os_ver_cmd = "cat /etc/redhat-release"

    if os_ver_from:
        curr_os_ver = utils.system_output(os_ver_cmd)
        if os_ver_from not in curr_os_ver:
            raise error.TestNAError("The current OS is %s" % curr_os_ver)

    if os_ver_to:
        status, curr_os_ver = run_remote_cmd(os_ver_cmd, server_ip,
                                             server_user, server_pwd)
        if os_ver_to not in curr_os_ver:
            raise error.TestNAError("The current OS is %s" % curr_os_ver)

    # Get the first disk source path
    first_disk = vm.get_first_disk_devices()
    disk_source = first_disk['source']
    logging.debug("disk source: %s", disk_source)
    curr_vm_xml = utils.system_output('cat %s' % vmxml_backup.xml)
    logging.debug("The current VM XML contents: \n%s", curr_vm_xml)
    orig_image_name = os.path.basename(disk_source)

    iscsi_setup = "yes" == test_dict.get("iscsi_setup", "no")
    disk_format = test_dict.get("disk_format", "qcow2")
#    primary_target = vm.get_first_disk_devices()["target"]
#    file_path, file_size = vm.get_device_size(primary_target)

    nfs_serv = None
    nfs_cli = None
    se_obj = None
    libvirtd_conf = None
    n_server_c = None
    n_client_c = None
    n_server_s = None
    n_client_s = None
    need_mkswap = False
    LOCAL_SELINUX_ENFORCING = True
    REMOTE_SELINUX_ENFORCING = True

    try:
        if iscsi_setup:
            target = libvirt.setup_or_cleanup_iscsi(is_setup=True, is_login=False,
                                                    emulated_image="emulated_iscsi",
                                                    portal_ip=portal_ip)
            logging.debug("Created iscsi target: %s", target)
            host_ip = None
            ipv6_addr_src = params.get("ipv6_addr_src")
            if ipv6_addr_src:
                host_ip = ipv6_addr_src
            else:
                host_ip = client_ip
            build_disk_xml(vm_name, disk_format, host_ip, disk_src_protocol,
                           target, transport=iscsi_transport)

            vmxml_iscsi = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
            curr_vm_xml = utils.system_output('cat %s' % vmxml_iscsi.xml)
            logging.debug("The current VM XML contents: \n%s", curr_vm_xml)

        del_vm_video_dev = "yes" == test_dict.get("del_vm_video_dev", "no")
        if del_vm_video_dev:
            delete_video_device(vm_name)

        iface_address = test_dict.get("iface_address")
        if iface_address:
            update_interface_xml(vm_name, iface_address)

        if sound_model:
            logging.info("Prepare to update VM's sound XML")
            update_sound_device(vm_name, sound_model)

        watchdog_model = test_dict.get("watchdog_model")
        watchdog_action = test_dict.get("watchdog_action", "none")
        watchdog_module_args = test_dict.get("watchdog_module_args", "")
        if watchdog_model:
            prepare_guest_watchdog(vm_name, vm, watchdog_model, watchdog_action,
                                   watchdog_module_args)
            curr_vm_xml = utils.system_output('cat %s' % vmxml_backup.xml)
            logging.debug("The current VM XML contents: \n%s", curr_vm_xml)

        smartcard_mode = test_dict.get("smartcard_mode")
        smartcard_type = test_dict.get("smartcard_type")
        if smartcard_mode and smartcard_type:
            add_smartcard_device(vm_name, smartcard_type, smartcard_mode)
            curr_vm_xml = utils.system_output('cat %s' % vmxml_backup.xml)
            logging.debug("The current VM XML contents: \n%s", curr_vm_xml)

        pm_mem_enabled = test_dict.get("pm_mem_enabled", "no")
        pm_disk_enabled = test_dict.get("pm_disk_enabled", "no")
        suspend_target = test_dict.get("pm_suspend_target")
        if suspend_target:
            logging.info("Prepare to add VM's agent XML")
            vmxml_backup.set_pm_suspend(vm_name, pm_mem_enabled, pm_disk_enabled)

        config_vm_agent = "yes" == test_dict.get("config_vm_agent", "no")
        if config_vm_agent:
            vm.prepare_guest_agent()
            vm.setenforce(0)

        if nfs_mount_dir:
            cmd = "mkdir -p %s" % nfs_mount_dir
            logging.debug("Make sure %s exists both local and remote", nfs_mount_dir)
            output = utils.system_output(cmd)
            if output:
                raise error.TestFail("Failed to run '%s' on the local : %s"
                                     % (cmd, output))

            status, output = run_remote_cmd(cmd, server_ip, server_user, server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

        cpu_model = test_dict.get("cpu_model_name")
        cpu_vendor = test_dict.get("cpu_vendor")
        cpu_feature_dict = eval(test_dict.get("cpu_feature_dict", "{}"))
        cpu_mode = test_dict.get("cpu_mode", "custom")
        cpu_match = test_dict.get("cpu_match", "exact")
        cpu_model_fallback = test_dict.get("cpu_model_fallback", "allow")
        if cpu_model and cpu_vendor:
            custom_cpu(vm_name, cpu_model, cpu_vendor, cpu_model_fallback,
                       cpu_feature_dict, cpu_mode, cpu_match)

        # Update VM disk source to NFS sharing directory
        logging.debug("Migration mounting point: %s", nfs_mount_dir)
        new_disk_source = test_dict.get("new_disk_source")
        if nfs_mount_dir and nfs_mount_dir != os.path.dirname(disk_source):
            libvirt.update_vm_disk_source(vm_name, nfs_mount_dir, "", source_type)

        target_image_path = test_dict.get("target_image_path")
        target_image_name = test_dict.get("target_image_name", "")
        if new_disk_source and target_image_path:
            libvirt.update_vm_disk_source(vm_name, target_image_path,
                                          target_image_name, source_type)

        vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
        logging.debug("The VM XML with new disk source: \n%s", vm_xml_cxt)

        # Prepare to update VM first disk driver cache
        disk_name = test_dict.get("disk_driver_name")
        disk_type = test_dict.get("disk_driver_type")
        disk_cache = test_dict.get("disk_driver_cache")
        if disk_name or disk_type or disk_cache:
            update_disk_driver(vm_name, disk_name, disk_type, disk_cache)

        image_info_dict = utils_misc.get_image_info(disk_source)
        logging.debug("disk image info: %s", image_info_dict)
        target_image_source = test_dict.get("target_image_source", disk_source)
        if create_target_image:
            if not target_image_size and image_info_dict:
                target_image_size = image_info_dict.get('dsize')
            if not target_image_format and image_info_dict:
                target_image_format = image_info_dict.get('format')
            if target_image_size and target_image_format:
                # Make sure the target image path exists
                cmd = "mkdir -p %s " % os.path.dirname(target_image_source)
                cmd += "&& qemu-img create -f %s %s %s" % (target_image_format,
                                                           target_image_source,
                                                           target_image_size)
                status, output = run_remote_cmd(cmd, server_ip, server_user, server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

                remote_image_list.append(target_image_source)

        cmd = test_dict.get("create_another_target_image_cmd")
        if cmd:
            status, output = run_remote_cmd(cmd, server_ip, server_user, server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

            remote_image_list.append(target_disk_image)

        # Process domain disk device parameters
        gluster_disk = "yes" == test_dict.get("gluster_disk")
        vol_name = test_dict.get("vol_name")
        default_pool = test_dict.get("default_pool", "")
        pool_name = test_dict.get("pool_name")
        if pool_name:
            test_dict['brick_path'] = os.path.join(test.virtdir, pool_name)

        if gluster_disk:
            logging.info("Put local SELinux in permissive mode")
            utils_selinux.set_status("permissive")
            LOCAL_SELINUX_ENFORCING = False

            logging.info("Put remote SELinux in permissive mode")
            cmd = "setenforce permissive"
            status, output = run_remote_cmd(cmd, server_ip, server_user, server_pwd)
            if status:
                raise error.TestNAError("Failed to set SELinux in permissive mode")

            REMOTE_SELINUX_ENFORCING = False

            # Setup glusterfs and disk xml.
            disk_img = "gluster.%s" % disk_format
            test_dict['disk_img'] = disk_img
            host_ip = prepare_gluster_disk(test_dict)
            build_disk_xml(vm_name, disk_format, host_ip, disk_src_protocol,
                           vol_name, disk_img, gluster_transport)

            vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
            logging.debug("The VM XML with gluster disk source: \n%s", vm_xml_cxt)

        # generate remote IP
        if target_ip == "":
            if config_ipv6 == "yes" and ipv6_addr_des:
                target_ip = "[%s]" % ipv6_addr_des
            elif config_ipv6 != "yes" and server_cn:
                target_ip = server_cn
            elif config_ipv6 != "yes" and ipv6_addr_des:
                target_ip = "[%s]" % ipv6_addr_des
            elif server_ip:
                target_ip = server_ip
            else:
                target_ip = target_ip

        # generate URI
        uri = "%s%s%s://%s%s%s" % (driver, plus, transport,
                                   target_ip, uri_port, uri_path)
        test_dict["desuri"] = uri

        logging.debug("The final test dict:\n<%s>", test_dict)

        if diff_cpu_vendor:
            local_vendor = utils_misc.get_cpu_vendor()
            logging.info("Local CPU vendor: %s", local_vendor)
            local_cpu_xml = get_cpu_xml_from_virsh_caps()
            logging.debug("Local CPU XML: \n%s", local_cpu_xml)

            cmd = "grep %s /proc/cpuinfo" % local_vendor
            session, status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                     server_pwd, ret_status_output=False,
                                                     ret_session_status_output=True)
            try:
                if not status:
                    raise error.TestNAError("The CPU model is the same between local "
                                            "and remote host %s:%s" % (local_vendor,
                                                                       output))
                if not session:
                    raise error.TestFail("The session is dead!!")

                runner = session.cmd_output
                remote_cpu_xml = get_cpu_xml_from_virsh_caps(runner)
                session.close()
                logging.debug("Remote CPU XML: \n%s", remote_cpu_xml)
                cpu_xml = os.path.join(test.tmpdir, 'cpu.xml')
                fp = open(cpu_xml, "w+")
                fp.write(local_cpu_xml)
                fp.write("\n")
                fp.write(remote_cpu_xml)
                fp.close()
                cpu_xml_cxt = utils.system_output("cat %s" % cpu_xml)
                logging.debug("The CPU XML contents: \n%s", cpu_xml_cxt)
                cmd = "sed -i '/<vendor>.*<\/vendor>/d' %s" % cpu_xml
                utils.system(cmd)
                cpu_xml_cxt = utils.system_output("cat %s" % cpu_xml)
                logging.debug("The current CPU XML contents: \n%s", cpu_xml_cxt)
                output = compute_cpu_baseline(cpu_xml, status_error)
                logging.debug("The baseline CPU XML: \n%s", output)
                output = output.replace("\n", "")
                vm_new_xml = os.path.join(test.tmpdir, 'vm_new.xml')
                fp = open(vm_new_xml, "w+")
                fp.write(str(vmxml_backup))
                fp.close()
                vm_new_xml_cxt = utils.system_output("cat %s" % vm_new_xml)
                logging.debug("The current VM XML contents: \n%s", vm_new_xml_cxt)
                cpuxml = output
                cmd = 'sed -i "/<\/features>/ a\%s" %s' % (cpuxml, vm_new_xml)
                logging.debug("The command: %s", cmd)
                utils.system(cmd)
                vm_new_xml_cxt = utils.system_output("cat %s" % vm_new_xml)
                logging.debug("The new VM XML contents: \n%s", vm_new_xml_cxt)
                virsh.define(vm_new_xml)
                vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
                logging.debug("The current VM XML contents: \n%s", vm_xml_cxt)
            finally:
                logging.info("Recovery VM XML configration")
                vmxml_backup.sync()
                logging.debug("The current VM XML:\n%s", vmxml_backup.xmltreefile)

        if cpu_set:
            vcpu_args = ""
            if vcpu_cpuset:
                vcpu_args += "cpuset='%s'" % vcpu_cpuset
            edit_cmd = []
            update_cmd = r":%s/<vcpu.*>[0-9]*<\/vcpu>/<vcpu "
            update_cmd += vcpu_args + ">" + vcpu_num + r"<\/vcpu>"
            edit_cmd.append(update_cmd)
            libvirt.exec_virsh_edit(vm_name, edit_cmd)
            vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
            logging.debug("The current VM XML contents: \n%s", vm_xml_cxt)

        # setup IPv6
        if config_ipv6 == "yes":
            ipv6_obj = IPv6Manager(test_dict)
            objs_list.append(ipv6_obj)
            ipv6_obj.setup()

        # setup SSH
        if transport == "ssh" and setup_ssh == "yes":
            ssh_obj = SSHConnection(test_dict)
            if ssh_recovery == "yes":
                objs_list.append(ssh_obj)
            # setup test environment
            ssh_obj.conn_setup()

        # setup TLS
        if transport == "tls" and setup_tls == "yes":
            tls_obj = TLSConnection(test_dict)
            if tls_recovery == "yes":
                objs_list.append(tls_obj)
            # setup CA, server and client
            tls_obj.conn_setup()

        # setup TCP
        if transport == "tcp" and setup_tcp == "yes":
            tcp_obj = TCPConnection(test_dict)
            if tcp_recovery == "yes":
                objs_list.append(tcp_obj)
            # setup test environment
            tcp_obj.conn_setup()

        # check TCP/IP listening by service
        if restart_libvirtd != "no":
            service = 'libvirtd'
            if transport == "ssh":
                service = 'ssh'

            check_listening_port_remote_by_service(server_ip, server_user,
                                                   server_pwd, service,
                                                   '22', listen_addr)

        # add a user
        if adduser_cmd:
            utils.system(adduser_cmd, ignore_status=True)

        # update libvirtd config with new host_uuid
        if config_libvirtd == "yes":
            if host_uuid:
                libvirtd_conf_dict["host_uuid"] = host_uuid
            libvirtd_conf = config_libvirt(libvirtd_conf_dict)

            if libvirtd_conf:
                local_path = libvirtd_conf.conf_path
                remote.scp_to_remote(server_ip, '22', server_user,
                                     server_pwd, local_path, remote_path,
                                     limit="", log_filename=None,
                                     timeout=600, interface=None)

                libvirt.remotely_control_libvirtd(server_ip, server_user,
                                                  server_pwd, action='restart',
                                                  status_error='no')

        # need to remotely stop libvirt service for negative testing
        if stop_libvirtd_remotely:
            libvirt.remotely_control_libvirtd(server_ip, server_user,
                                              server_pwd, "stop", status_error)

        if setup_nfs:
            logging.info("Setup NFS test environment...")
            nfs_serv = nfs.Nfs(test_dict)
            nfs_serv.setup()
            nfs_cli = nfs.NFSClient(test_dict)
            nfs_cli.setup()

        if enable_virt_use_nfs:
            logging.info("Enable virt NFS SELinux boolean")
            se_obj = SELinuxBoolean(test_dict)
            se_obj.setup()

        if mb_enable:
            logging.info("Add memoryBacking into VM XML")
            vm_xml.VMXML.set_memoryBacking_tag(vm_name)
            vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
            logging.debug("The current VM XML: \n%s", vm_xml_cxt)

        if config_remote_hugepages:
            cmds = ["mkdir -p %s" % remote_hugetlbfs_path,
                    "mount -t hugetlbfs none %s" % remote_hugetlbfs_path,
                    "sysctl vm.nr_hugepages=%s" % int(remote_tgt_hugepages)]
            for cmd in cmds:
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))
            HUGETLBFS_MOUNT = True

        if create_disk_src_backing_file:
            cmd = create_disk_src_backing_file + orig_image_name
            out = utils.system_output(cmd, ignore_status=True)
            if not out:
                raise error.TestFail("Failed to create backing file: %s" % cmd)
            logging.info(out)
            local_image_list.append(new_disk_source)

        if create_disk_tgt_backing_file:
            cmd = create_disk_src_backing_file + orig_image_name
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

            remote_image_list.append(new_disk_source)

        if restart_libvirtd_remotely:
            cmd = "service libvirtd restart"
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

        if remote_hugetlbfs_path:
            cmd = "ls %s" % remote_hugetlbfs_path
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

        if memtune_options:
            virsh.memtune_set(vm_name, memtune_options)
            vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
            logging.debug("The VM XML with memory tune: \n%s", vm_xml_cxt)

        check_image_size = "yes" == test_dict.get("check_image_size", "no")
        local_image_source = test_dict.get("local_image_source")
        tgt_size = 0
        if local_image_source and check_image_size:
            image_info_dict = utils_misc.get_image_info(local_image_source)
            logging.debug("Local disk image info: %s", image_info_dict)

            dsize = image_info_dict["dsize"]
            dd_image_count = int(test_dict.get("dd_image_count"))
            dd_image_bs = int(test_dict.get("dd_image_bs"))
            dd_image_size = dd_image_count * dd_image_bs
            tgt_size = dsize + dd_image_size
            logging.info("Expected disk image size: %s", tgt_size)

        if dev_type_list:
            for dev_type in dev_type_list:
                image_size = ""
                if not source_file:
                    source_file = "%s/virt_%s.img" % (nfs_mount_dir, dev_type)
                logging.debug("Disk source: %s", source_file)
                if cdrom_image_size and dev_type == 'cdrom':
                    image_size = cdrom_image_size
                if floppy_image_size and dev_type == 'floppy':
                    image_size = floppy_image_size
                if image_size:
                    local_image_list.append(source_file)
                    disk_xml = add_disk_xml(dev_type, source_file,
                                            image_size, policy)
                else:
                    cdrom_disk_type = test_dict.get("cdrom_disk_type")
                    disk_xml = add_disk_xml(dev_type, source_file,
                                            image_size, policy,
                                            cdrom_disk_type)

                logging.debug("Disk XML: %s", disk_xml)
                if disk_xml and os.path.isfile(disk_xml):
                    virsh_dargs = {'debug': True, 'ignore_status': True}
                    virsh.attach_device(domainarg=vm_name, filearg=disk_xml,
                                        flagstr="--config", **virsh_dargs)
                    utils.run("rm -f %s" % disk_xml, ignore_status=True)

                vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
                logging.debug("The VM XML with attached disk: \n%s", vm_xml_cxt)

        if local_image and not os.path.exists(local_image):
            image_fmt = test_dict.get("local_image_format", "raw")
            disk_size = test_dict.get("local_disk_size", "10M")
            attach_args = test_dict.get("attach_disk_args")
            image_cmd = "qemu-img create -f %s %s %s" % (image_fmt,
                                                         local_image,
                                                         disk_size)
            logging.info("Create image for disk: %s", image_cmd)
            utils.run(image_cmd)
            local_image_list.append(local_image)

            setup_loop_cmd = test_dict.get("setup_loop_dev_cmd")
            mk_loop_fmt = test_dict.get("mk_loop_dev_format_cmd")
            if setup_loop_cmd and mk_loop_fmt:
                utils.system_output(setup_loop_cmd, ignore_status=False)
                utils.system_output(mk_loop_fmt, ignore_status=False)

                status, output = run_remote_cmd(setup_loop_cmd, server_ip,
                                                server_user, server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (setup_loop_cmd, output))

            if attach_args:
                logging.info("Prepare to attach disk to guest")
                c_attach = virsh.attach_disk(vm_name, local_image, target_dev,
                                             attach_args, debug=True)
                if c_attach.exit_status != 0:
                    logging.error("Attach disk failed before test.")

                vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
                logging.debug("The VM XML with attached disk: \n%s", vm_xml_cxt)

            attach_disk = True

        start_filter_string = test_dict.get("start_filter_string")
        # start vm and prepare to migrate
        vm_session = None
        if not vm.is_alive() or vm.is_dead():
            result = None
            try:
                vm.start()
            except virt_vm.VMStartError, e:
                logging.info("Recovery VM XML configration")
                vmxml_backup.sync()
                logging.debug("The current VM XML:\n%s", vmxml_backup.xmltreefile)
                if start_filter_string:
                    if re.search(start_filter_string, str(e)):
                        raise error.TestNAError("Failed to start VM: %s" % e)
                    else:
                        raise error.TestFail("Failed to start VM: %s" % e)
                else:
                    raise error.TestFail("Failed to start VM: %s" % e)

            if disk_src_protocol != "iscsi":
                vm_session = vm.wait_for_login()

        guest_cmd = test_dict.get("guest_cmd")
        if guest_cmd and vm_session:
            status, output = vm_session.cmd_status_output(guest_cmd)
            logging.debug("To run '%s' in VM: status=<%s>, output=<%s>",
                          guest_cmd, status, output)
            if status:
                raise error.TestFail("Failed to run '%s' : %s"
                                     % (guest_cmd, output))
            logging.info(output)

        if pause_vm:
            if not vm.pause():
                raise error.TestFail("Guest state should be"
                                     " paused after started"
                                     " because of initia guest state")
        if reboot_vm:
            vm.reboot()

        if remote_port:
            cmd = "nc -l -p %s &" % remote_port
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

        if get_migr_cache:
            check_virsh_command_and_option("migrate-compcache")
            result = virsh.migrate_compcache(vm_name)
            logging.debug(result)

        if (total_bytes_sec or read_bytes_sec or write_bytes_sec or
                total_iops_sec or read_iops_sec or write_iops_sec) and blkdevio_dev:
            result = virsh.blkdeviotune(vm_name, blkdevio_dev, blkdevio_options,
                                        total_bytes_sec, read_bytes_sec,
                                        write_bytes_sec, total_iops_sec,
                                        read_iops_sec, write_iops_sec)
            libvirt.check_exit_status(result)

        if no_swap and vm_session:
            cmd = "swapon -s"
            logging.info("Execute command <%s> in the VM", cmd)
            status, output = vm_session.cmd_status_output(cmd, timeout=600)
            if status or output:
                raise error.TestFail("Failed to run %s in VM: %s"
                                     % (cmd, output))
            logging.debug(output)

            cmd = test_dict.get("memhog_install_pkg")
            logging.info("Execute command <%s> in the VM", cmd)
            status, output = vm_session.cmd_status_output(cmd, timeout=600)
            if status:
                raise error.TestFail("Failed to run %s in VM: %s"
                                     % (cmd, output))
            logging.debug(output)

            # memory size should be less than VM's physical memory
            mem_size = current_mem - 100
            cmd = "memhog -r20 %s" % mem_size
            logging.info("Execute command <%s> in the VM", cmd)
            status, output = vm_session.cmd_status_output(cmd, timeout=600)
            if status:
                raise error.TestFail("Failed to run %s in VM: %s"
                                     % (cmd, output))
            logging.debug(output)

        run_cmd_in_vm = test_dict.get("run_cmd_in_vm")
        if run_cmd_in_vm and vm_session:
            logging.info("Execute command <%s> in the VM", run_cmd_in_vm)
            status, output = vm_session.cmd_status_output(run_cmd_in_vm)
            if status:
                raise error.TestFail("Failed to run %s in VM: %s"
                                     % (run_cmd_in_vm, output))
            logging.debug(output)

        if stress_args:
            s_list = stress_type.split("_")

            if s_list and s_list[-1] == "vms":
                if not vm_session:
                    raise error.TestFail("The VM session is inactive!!")
                else:
                    cmd = "yum install patch -y"
                    logging.info("Run '%s' in VM", cmd)
                    status, output = vm_session.cmd_status_output(cmd,
                                                                  timeout=600)
                    if status:
                        raise error.TestFail("Failed to run %s in VM: %s"
                                             % (cmd, output))
                    logging.debug(output)

            elif s_list and s_list[-1] == "host":
                logging.info("Run '%s %s' in %s", s_list[0],
                             stress_args, s_list[-1])
                err_msg = utils_test.load_stress(stress_type,
                                                 [vm], test_dict)
                if len(err_msg):
                    raise error.TestFail("Add stress for migration failed:%s"
                                         % err_msg[0])
            else:
                raise error.TestFail("The stress type looks like "
                                     "'stress_in_vms, iozone_in_vms, "
                                     "stress_on_host'!!")

        if set_migr_cache_size:
            check_virsh_command_and_option("migrate-compcache")
            result = virsh.migrate_compcache(vm_name, size=set_migr_cache_size)
            logging.debug(result)

        netperf_version = test_dict.get("netperf_version")
        if netperf_version:
            ret, n_client_c, n_server_c = setup_netsever_and_launch_netperf(test_dict)
            if not ret:
                raise error.TestError("Can not start netperf on %s!!" % client_ip)

            new_args_dict = dict(test_dict)
            new_args_dict["server_ip"] = client_ip
            new_args_dict["server_user"] = client_user
            new_args_dict["server_pwd"] = client_pwd
            new_args_dict["client_ip"] = server_ip
            new_args_dict["client_user"] = server_user
            new_args_dict["client_pwd"] = server_pwd
            new_args_dict["server_md5sum"] = test_dict.get("client_md5sum")
            new_args_dict["server_path"] = test_dict.get("client_path", "/var/tmp")
            new_args_dict["compile_option_server"] = test_dict.get("compile_option_client", "")
            new_args_dict["client_md5sum"] = test_dict.get("server_md5sum")
            new_args_dict["client_path"] = test_dict.get("server_path", "/var/tmp")
            new_args_dict["compile_option_client"] = test_dict.get("compile_option_server", "")

            ret, n_client_s, n_server_s = setup_netsever_and_launch_netperf(new_args_dict)
            if not ret:
                raise error.TestError("Can not start netperf on %s!!" % client_ip)

        speed = test_dict.get("set_migration_speed")
        if speed:
            cmd = "migrate-setspeed"
            if not virsh.has_help_command(cmd):
                raise error.TestNAError("This version of libvirt does not support "
                                        "virsh command %s" % cmd)

            logging.debug("Set migration speed to %s", speed)
            virsh.migrate_setspeed(vm_name, speed)

        iface_num = int(test_dict.get("attach_iface_times", 0))
        if iface_num > 0:
            for i in range(int(iface_num)):
                logging.info("Try to attach interface loop %s" % i)
                options = test_dict.get("attach_iface_options", "")
                ret = virsh.attach_interface(vm_name, options,
                                             ignore_status=True)
                if ret.exit_status:
                    if ret.stderr.count("No more available PCI slots"):
                        break
                    elif status_error:
                        continue
                    else:
                        logging.error("Command output %s" %
                                      ret.stdout.strip())
                        raise error.TestFail("Failed to attach-interface")
            vm_xml_cxt = utils.system_output("virsh dumpxml %s" % vm_name)
            logging.debug("The VM XML with attached interface: \n%s",
                          vm_xml_cxt)

        set_src_pm_suspend_tgt = test_dict.get("set_src_pm_suspend_target")
        set_src_pm_wakeup = "yes" == test_dict.get("set_src_pm_wakeup", "no")
        if set_src_pm_suspend_tgt:
            tgts = set_src_pm_suspend_tgt.split(",")
            for tgt in tgts:
                tgt = tgt.strip()
                if tgt == "disk" or tgt == "hybrid":
                    if vm.is_dead():
                        vm.start()
                    need_mkswap = not vm.has_swap()
                    if need_mkswap:
                        logging.debug("Creating swap partition")
                        swap_path = test_dict.get("swap_path")
                        vm.create_swap_partition(swap_path)

                result = virsh.dompmsuspend(vm_name, tgt, ignore_status=True,
                                            debug=True)
                libvirt.check_exit_status(result)
                if (tgt == "mem" or tgt == "hybrid") and set_src_pm_wakeup:
                    result = virsh.dompmwakeup(vm_name, ignore_status=True,
                                               debug=True)
                    libvirt.check_exit_status(result)
            logging.debug("Current VM state: <%s>", vm.state())
            if vm.state() == "in shutdown":
                vm.wait_for_shutdown()
            if vm.is_dead():
                vm.start()
                vm.wait_for_login()

        if run_migr_back:
            options = test_dict.get("virsh_options", "--verbose --live")
            command = "virsh migrate %s %s %s" % (vm_name, options, uri)
            logging.debug("Start migrating: %s", command)
            p = Popen(command, shell=True, stdout=PIPE, stderr=PIPE)

            # wait for live storage migration starting
            time.sleep(delay)

            if ctrl_c:
                if p.pid:
                    logging.info("Send SIGINT signal to cancel migration.")
                    utils_misc.kill_process_tree(p.pid, signal.SIGINT)
                    logging.info("Succeed to cancel migration: [%s].", p.pid)
                else:
                    p.kill()
                    raise error.TestFail("Migration process is dead!!")

            if check_domain_state:
                domain_state = virsh.domstate(vm_name, debug=True).stdout.strip()
                if expected_domain_state != domain_state:
                    raise error.TestFail("The domain state is not expected: %s"
                                         % domain_state)

            # Give enough time for starting job
            t = 0
            jobinfo = None
            jobtype = "None"
            options = ""
            check_time = int(test_dict.get("check_job_info_time", 10))
            if check_job_info:
                while t < check_time:
                    jobinfo = virsh.domjobinfo(vm_name, debug=True,
                                               ignore_status=True).stdout
                    logging.debug("Job info: %s", jobinfo)
                    for line in jobinfo.splitlines():
                        key = line.split(':')[0]
                        if key.count("type"):
                            jobtype = line.split(':')[-1].strip()
                    if "None" == jobtype:
                        t += 1
                        time.sleep(1)
                        continue
                    else:
                        break

                if check_complete_job == "yes":
                    stdout, stderr = p.communicate()
                    logging.info("stdout:[%s], stderr:[%s]", stdout, stderr)
                    opts = "--completed"
                    args = vm_name + " " + opts
                    check_virsh_command_and_option("domjobinfo", opts)
                    jobinfo = virsh.domjobinfo(args, debug=True,
                                               ignore_status=True).stdout
                    logging.debug("Local job info: %s", jobinfo)
                    cmd = "virsh domjobinfo %s %s" % (vm_name, opts)
                    logging.debug("Get remote job info")
                    status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                    server_pwd)
                    if status or not re.search(jobinfo, output):
                        raise error.TestFail("Failed to run '%s' on the remote"
                                             " : %s" % (cmd, output))

            if block_ip_addr and block_time:
                block_specific_ip_by_time(block_ip_addr, block_time)
                add_iptables_rules = True

                stdout, stderr = p.communicate()
                logging.info("stdout:<%s> , stderr:<%s>", stdout, stderr)

            if abort_job and jobtype != "None":
                job_ret = virsh.domjobabort(vm_name, debug=True)
                if job_ret.exit_status:
                    raise error.TestError("Failed to abort active domain job.")
                else:
                    stderr = p.communicate()[1]
                    logging.debug(stderr)
                    err_str = ".*error.*migration job: canceled by client"
                    if not re.search(err_str, stderr):
                        raise error.TestFail("Can't find error: %s." % (err_str))
                    else:
                        logging.info("Find error: %s.", err_str)

            max_down_time = test_dict.get("max_down_time")
            if max_down_time:
                result = virsh.migrate_setmaxdowntime(vm_name, max_down_time)
                if result.exit_status:
                    logging.error("Set max migration downtime failed.")
                logging.debug(result)

            sleep_time = test_dict.get("sleep_time")
            kill_cmd = test_dict.get("kill_command")
            if sleep_time:
                logging.info("Sleep %s(s)", sleep_time)
                time.sleep(int(sleep_time))

            if kill_cmd:
                logging.info("Execute %s on the host", kill_cmd)
                utils.system(kill_cmd)

            wait_for_mgr_cmpl = test_dict.get("wait_for_migration_complete", "no")
            if wait_for_mgr_cmpl == "yes":
                stdout, stderr = p.communicate()
                logging.info("stdout:<%s> , stderr:<%s>", stdout, stderr)
                if stderr:
                    raise error.TestFail("Can't finish VM migration!!")

            if p.poll():
                try:
                    p.kill()
                except OSError:
                    pass
        if run_migr_front:
            migrate_vm(test_dict)
            logging.info("Succeed to migrate %s.", vm_name)

        set_tgt_pm_suspend_tgt = test_dict.get("set_tgt_pm_suspend_target")
        set_tgt_pm_wakeup = "yes" == test_dict.get("set_tgt_pm_wakeup", "no")
        state_delay = int(test_dict.get("target_state_delay", 0))
        if set_tgt_pm_suspend_tgt:
            tgts = set_tgt_pm_suspend_tgt.split(",")
            for tgt in tgts:
                cmd = "virsh dompmsuspend %s --target %s" % (vm_name, tgt)
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

                if tgt == "mem" and set_tgt_pm_wakeup:
                    cmd = "virsh dompmwakeup %s" % vm_name
                    status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                    server_pwd)
                    if status:
                        raise error.TestFail("Failed to run '%s' on the "
                                             "remote: %s" % (cmd, output))

        run_cmd_in_vm = test_dict.get("run_cmd_in_vm_after_migration")
        if run_cmd_in_vm:
            vm_ip = vm.get_address()
            vm_pwd = test_dict.get("password")
            logging.debug("The VM IP: <%s> password: <%s>", vm_ip, vm_pwd)
            logging.info("Execute command <%s> in the VM after migration",
                         run_cmd_in_vm)

            remote_vm_obj = utils_test.RemoteVMManager(test_dict)
            remote_vm_obj.check_network(vm_ip)
#            remote_vm_obj.setup_ssh_auth(vm_ip, vm_pwd, timeout=60)
#            remote_vm_obj.run_command(vm_ip, run_cmd_in_vm)

        cmd = test_dict.get("check_disk_size_cmd")
        if cmd and check_image_size:
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

            logging.debug("Remote disk image info: %s", output)

        cmd = test_dict.get("target_qemu_filter")
        if cmd:
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))
            logging.debug("The filtered result:\n%s", output)

        if restart_libvirtd == "yes":
            libvirtd = utils_libvirtd.Libvirtd()
            libvirtd.restart()

        if restart_vm:
            vm.destroy()
            vm.start()
            vm.wait_for_login()

        if pause_vm:
            cmd = "virsh domstate %s" % vm_name
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status or not re.search("paused", output):
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))

        grep_str_local = test_dict.get("grep_str_from_local_libvirt_log")
        if config_libvirtd == "yes" and grep_str_local:
            cmd = "grep -E '%s' %s" % (grep_str_local, log_file)
            logging.debug("Execute command %s: %s", cmd, utils.system_output(cmd))

        grep_str_remote = test_dict.get("grep_str_from_remote_libvirt_log")
        if grep_str_remote:
            cmd = "grep %s %s" % (grep_str_remote, log_file)
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            logging.debug("The command result: %s", output)
            if status:
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))
        if migr_vm_back:
            options = test_dict.get("virsh_options", "--verbose --live")
            src_uri = test_dict.get("migration_source_uri")
            cmd = "virsh migrate %s %s %s" % (vm_name,
                                              options, src_uri)
            logging.debug("Start migrating: %s", cmd)
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                destroy_cmd = "virsh destroy %s" % vm_name
                run_remote_cmd(destroy_cmd, server_ip,
                               server_user, server_pwd)
                raise error.TestFail("Failed to run '%s' on the remote: %s"
                                     % (cmd, output))
            logging.info(output)

    finally:
        logging.info("Recovery test environment")

        if need_mkswap:
            if not vm.is_alive() or vm.is_dead():
                vm.start()
                vm.wait_for_login()
                vm.cleanup_swap()

        if not LOCAL_SELINUX_ENFORCING:
            logging.info("Put SELinux in enforcing mode")
            utils_selinux.set_status("enforcing")

        if not REMOTE_SELINUX_ENFORCING:
            logging.info("Put remote SELinux in enforcing mode")
            cmd = "setenforce enforcing"
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)
            if status:
                raise error.TestNAError("Failed to set SELinux in enforcing mode, %s" % output)

        # Delete all rules in chain or all chains
        if add_iptables_rules:
            utils.run("iptables -F", ignore_status=True)

        # Restore libvirtd conf and restart libvirtd
        if libvirtd_conf:
            libvirtd_conf.restore()
            libvirtd = utils_libvirtd.Libvirtd()
            libvirtd.restart()
            local_path = libvirtd_conf.conf_path
            remote.scp_to_remote(server_ip, '22', server_user, server_pwd,
                                 local_path, remote_path, limit="",
                                 log_filename=None, timeout=600, interface=None)

            libvirt.remotely_control_libvirtd(server_ip, server_user,
                                              server_pwd, action='restart',
                                              status_error='no')

        if deluser_cmd:
            utils.run(deluser_cmd, ignore_status=True)

        if local_image_list:
            for img_file in local_image_list:
                if os.path.exists(img_file):
                    utils.run("rm -f %s" % img_file, ignore_status=True)

        # Recovery remotely libvirt service
        if stop_libvirtd_remotely:
            libvirt.remotely_control_libvirtd(server_ip, server_user,
                                              server_pwd, "start", status_error)

        #if status_error == "no" and MIGRATE_RET and stop_remote_guest and not migr_vm_back:
        if status_error == "no":
            cmd = "virsh domstate %s" % vm_name
            status, output = run_remote_cmd(cmd, server_ip, server_user,
                                            server_pwd)

            if not status and output.strip() in ("running", "idle", "paused", "no state"):
                cmd = "virsh destroy %s" % vm_name
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

            virsh_options = test_dict.get("virsh_options", "--live --verbose")
            if not status and re.search("--persistent", virsh_options):
                cmd = "virsh undefine %s" % vm_name
                match_string = "Domain %s has been undefined" % vm_name
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status or not re.search(match_string, output):
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

        libvirtd = utils_libvirtd.Libvirtd()
        if disk_src_protocol == "gluster":
            libvirt.setup_or_cleanup_gluster(False, vol_name, brick_path)
            libvirtd.restart()

        if disk_src_protocol == "iscsi":
            libvirt.setup_or_cleanup_iscsi(is_setup=False)
            libvirtd.restart()

        logging.info("Recovery VM XML configration")
        vmxml_backup.sync()
        logging.debug("The current VM XML:\n%s", vmxml_backup.xmltreefile)

        if se_obj:
            logging.info("Recover virt NFS SELinux boolean")
            # Keep .ssh/authorized_keys for NFS cleanup later
            se_obj.cleanup(True)

        if nfs_serv and nfs_cli:
            logging.info("Cleanup NFS test environment...")
            nfs_serv.unexportfs_in_clean = True
            nfs_cli.cleanup()
            nfs_serv.cleanup()

        if mb_enable:
            vm_xml.VMXML.del_memoryBacking_tag(vm_name)

        if remote_image_list:
            for img_file in remote_image_list:
                cmd = "rm -rf %s" % img_file
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

        # vms will be shutdown, so no need to do this cleanup
        # And migrated vms may be not login if the network is local lan
        if stress_type == "stress_on_host":
            logging.info("Unload stress from host")
            utils_test.unload_stress(stress_type, [vm])

        if HUGETLBFS_MOUNT:
            cmds = ["umount -l %s" % remote_hugetlbfs_path,
                    "sysctl vm.nr_hugepages=0",
                    "service libvirtd restart"]
            for cmd in cmds:
                status, output = run_remote_cmd(cmd, server_ip, server_user,
                                                server_pwd)
                if status:
                    raise error.TestFail("Failed to run '%s' on the remote: %s"
                                         % (cmd, output))

        # Stop netserver service and clean up netperf package
        if n_server_c:
            n_server_c.stop()
            n_server_c.package.env_cleanup(True)
        if n_client_c:
            n_client_c.package.env_cleanup(True)

        if n_server_s:
            n_server_s.stop()
            n_server_s.package.env_cleanup(True)
        if n_client_s:
            n_client_s.package.env_cleanup(True)

        cleanup(objs_list)