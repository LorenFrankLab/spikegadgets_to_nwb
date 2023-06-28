from spikegadgets_to_nwb import convert_yaml, convert_rec_header

from pynwb.file import NWBFile
from ndx_franklab_novela import HeaderDevice


import os

path = os.path.dirname(os.path.abspath(__file__))


def xxxtest_add_header_device():  # TODO: resolve rec file loading and unhide test name
    # Set up test data
    metadata_path = path + "/test_data/test_metadata.yml"
    metadata, _ = convert_yaml.load_metadata(metadata_path, [])
    nwbfile = convert_yaml.initialize_nwb(metadata)
    recfile = "/stelmo/sam/test_data/minirec/raw/20230622/20230622_minirec_01_s1.rec"

    # Call the function to be tested
    convert_rec_header.add_header_device(nwbfile, recfile)

    # Perform assertions to check the results
    # Check if the device was added correctly
    assert "header_device" in nwbfile.devices
    header_device = nwbfile.devices["header_device"]
    assert isinstance(header_device, HeaderDevice)

    # Check if the device attributes were set correctly
    assert header_device.headstage_serial == "01504 00126"
    assert header_device.headstage_smart_ref_on == "0"
    assert header_device.realtime_mode == "0"
    assert header_device.headstage_auto_settle_on == "0"
    assert header_device.timestamp_at_creation == "51493215"
    assert header_device.controller_firmware_version == "3.18"
    assert header_device.controller_serial == "65535 65535"
    assert header_device.save_displayed_chan_only == "1"
    assert header_device.headstage_firmware_version == "4.4"
    assert header_device.qt_version == "6.2.2"
    assert header_device.compile_date == "May 24 2023"
    assert header_device.compile_time == "10:59:15"
    assert header_device.file_prefix == ""
    assert header_device.headstage_gyro_sensor_on == "1"
    assert header_device.headstage_mag_sensor_on == "0"
    assert header_device.trodes_version == "2.4.0"
    assert header_device.headstage_accel_sensor_on == "1"
    assert header_device.commit_head == "heads/Release_2.4.0-0-g499429f3"
    assert header_device.system_time_at_creation == "       1687474797888"
    assert header_device.file_path == ""
