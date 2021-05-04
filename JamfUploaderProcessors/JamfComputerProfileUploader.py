#!/usr/local/autopkg/python

"""
JamfComputerProfileUploader processor for uploading computer configuration profiles
to Jamf Pro using AutoPkg
    by G Pugh
"""

import json
import re
import os
import plistlib
import subprocess
import uuid

from collections import namedtuple
from base64 import b64encode
from pathlib import Path
from shutil import rmtree
from time import sleep
from xml.sax.saxutils import escape
from autopkglib import Processor, ProcessorError  # pylint: disable=import-error


class JamfComputerProfileUploader(Processor):
    """A processor for AutoPkg that will upload an item to a Jamf Cloud or on-prem server."""

    input_variables = {
        "JSS_URL": {
            "required": True,
            "description": "URL to a Jamf Pro server that the API user has write access "
            "to, optionally set as a key in the com.github.autopkg "
            "preference file.",
        },
        "API_USERNAME": {
            "required": True,
            "description": "Username of account with appropriate access to "
            "jss, optionally set as a key in the com.github.autopkg "
            "preference file.",
        },
        "API_PASSWORD": {
            "required": True,
            "description": "Password of api user, optionally set as a key in "
            "the com.github.autopkg preference file.",
        },
        "profile_name": {
            "required": False,
            "description": "Configuration Profile name",
            "default": "",
        },
        "payload": {
            "required": False,
            "description": "Path to Configuration Profile payload plist file",
        },
        "mobileconfig": {
            "required": False,
            "description": "Path to Configuration Profile mobileconfig file",
        },
        "identifier": {
            "required": False,
            "description": "Configuration Profile payload identifier",
        },
        "profile_template": {
            "required": False,
            "description": "Path to Configuration Profile XML template file",
        },
        "profile_category": {
            "required": False,
            "description": "a category to assign to the profile",
        },
        "organization": {
            "required": False,
            "description": "Organization to assign to the profile",
        },
        "profile_description": {
            "required": False,
            "description": "a description to assign to the profile",
        },
        "profile_computergroup": {
            "required": False,
            "description": "a computer group that will be scoped to the profile",
        },
        "unsign_profile": {
            "required": False,
            "description": (
                "Unsign a mobileconfig file prior to uploading "
                "if it is signed, if true."
            ),
            "default": False,
        },
        "replace_profile": {
            "required": False,
            "description": "overwrite an existing Configuration Profile if True.",
            "default": False,
        },
    }

    output_variables = {
        "jamfcomputerprofileuploader_summary_result": {
            "description": "Description of interesting results.",
        },
    }

    def write_json_file(self, data, tmp_dir="/tmp/jamf_upload"):
        """dump some json to a temporary file"""
        self.make_tmp_dir(tmp_dir)
        tf = os.path.join(tmp_dir, f"jamf_upload_{str(uuid.uuid4())}.json")
        with open(tf, "w") as fp:
            json.dump(data, fp)
        return tf

    def write_temp_file(self, data, tmp_dir="/tmp/jamf_upload"):
        """dump some text to a temporary file"""
        self.make_tmp_dir(tmp_dir)
        tf = os.path.join(tmp_dir, f"jamf_upload_{str(uuid.uuid4())}.txt")
        with open(tf, "w") as fp:
            fp.write(data)
        return tf

    def make_tmp_dir(self, tmp_dir="/tmp/jamf_upload"):
        """make the tmp directory"""
        if not os.path.exists(tmp_dir):
            os.mkdir(tmp_dir)
        return tmp_dir

    def clear_tmp_dir(self, tmp_dir="/tmp/jamf_upload"):
        """remove the tmp directory"""
        if os.path.exists(tmp_dir):
            rmtree(tmp_dir)
        return tmp_dir

    def curl(self, method, url, auth, data="", additional_headers=""):
        """
        build a curl command based on method (GET, PUT, POST, DELETE)
        If the URL contains 'uapi' then token should be passed to the auth variable,
        otherwise the enc_creds variable should be passed to the auth variable
        """
        tmp_dir = self.make_tmp_dir()
        headers_file = os.path.join(tmp_dir, "curl_headers_from_jamf_upload.txt")
        output_file = os.path.join(tmp_dir, "curl_output_from_jamf_upload.txt")
        cookie_jar = os.path.join(tmp_dir, "curl_cookies_from_jamf_upload.txt")

        # build the curl command
        curl_cmd = [
            "/usr/bin/curl",
            "-X",
            method,
            "-D",
            headers_file,
            "--output",
            output_file,
            url,
        ]

        # the authorisation is Basic unless we are using the uapi and already have a token
        if "uapi" in url and "tokens" not in url:
            curl_cmd.extend(["--header", f"authorization: Bearer {auth}"])
        else:
            curl_cmd.extend(["--header", f"authorization: Basic {auth}"])

        # set either Accept or Content-Type depending on method
        if method == "GET" or method == "DELETE":
            curl_cmd.extend(["--header", "Accept: application/json"])
        # icon upload requires special method
        elif method == "POST" and "fileuploads" in url:
            curl_cmd.extend(["--header", "Content-type: multipart/form-data"])
            curl_cmd.extend(["--form", f"name=@{data}"])
        elif method == "POST" or method == "PUT":
            if data:
                curl_cmd.extend(["--upload-file", data])
            # uapi sends json, classic API must send xml
            if "uapi" in url:
                curl_cmd.extend(["--header", "Content-type: application/json"])
            else:
                curl_cmd.extend(["--header", "Content-type: application/xml"])
        else:
            self.output(f"WARNING: HTTP method {method} not supported")

        # write session
        try:
            with open(headers_file, "r") as file:
                headers = file.readlines()
            existing_headers = [x.strip() for x in headers]
            for header in existing_headers:
                if "APBALANCEID" in header:
                    with open(cookie_jar, "w") as fp:
                        fp.write(header)
        except IOError:
            pass

        # look for existing session
        try:
            with open(cookie_jar, "r") as file:
                headers = file.readlines()
            existing_headers = [x.strip() for x in headers]
            for header in existing_headers:
                if "APBALANCEID" in header:
                    cookie = header.split()[1].rstrip(";")
                    self.output(f"Existing cookie found: {cookie}", verbose_level=2)
                    curl_cmd.extend(["--cookie", cookie])
        except IOError:
            self.output(
                "No existing cookie found - starting new session", verbose_level=2
            )

        # additional headers for advanced requests
        if additional_headers:
            curl_cmd.extend(additional_headers)

        self.output(f"curl command: {' '.join(curl_cmd)}", verbose_level=3)

        # now subprocess the curl command and build the r tuple which contains the
        # headers, status code and outputted data
        subprocess.check_output(curl_cmd)

        r = namedtuple(
            "r", ["headers", "status_code", "output"], defaults=(None, None, None)
        )
        try:
            with open(headers_file, "r") as file:
                headers = file.readlines()
            r.headers = [x.strip() for x in headers]
            for header in r.headers:  # pylint: disable=not-an-iterable
                if re.match(r"HTTP/(1.1|2)", header) and "Continue" not in header:
                    r.status_code = int(header.split()[1])
        except IOError:
            raise ProcessorError(f"WARNING: {headers_file} not found")
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            with open(output_file, "rb") as file:
                if "uapi" in url:
                    r.output = json.load(file)
                else:
                    r.output = file.read()
        else:
            self.output(f"No output from request ({output_file} not found or empty)")
        return r()

    def status_check(self, r, endpoint_type, obj_name):
        """Return a message dependent on the HTTP response"""
        if r.status_code == 200 or r.status_code == 201:
            self.output(f"{endpoint_type} '{obj_name}' uploaded successfully")
            return "break"
        elif r.status_code == 409:
            self.output(r.output, verbose_level=2)
            raise ProcessorError(
                f"WARNING: {endpoint_type} '{obj_name}' upload failed due to a conflict"
            )
        elif r.status_code == 401:
            raise ProcessorError(
                f"ERROR: {endpoint_type} '{obj_name}' upload failed due to permissions error"
            )
        else:
            raise ProcessorError(f"ERROR: {endpoint_type} '{obj_name}' upload failed")

    def get_path_to_file(self, filename):
        """AutoPkg is not very good at finding dependent files. This function will
        look inside the search directories for any supplied file """
        # if the supplied file is not a path, use the override directory or
        # ercipe dir if no override
        recipe_dir = self.env.get("RECIPE_DIR")
        filepath = os.path.join(recipe_dir, filename)
        if os.path.exists(filepath):
            self.output(f"File found at: {filepath}")
            return filepath

        # if not found, search RECIPE_SEARCH_DIRS to look for it
        search_dirs = self.env.get("RECIPE_SEARCH_DIRS")
        matched_filepath = ""
        for d in search_dirs:
            for path in Path(d).rglob(filename):
                matched_filepath = str(path)
                break
        if matched_filepath:
            self.output(f"File found at: {matched_filepath}")
            return matched_filepath

    def get_api_obj_id_from_name(self, jamf_url, object_name, enc_creds):
        """check if a Classic API object with the same name exists on the server"""
        # define the relationship between the object types and their URL
        url = f"{jamf_url}/JSSResource/osxconfigurationprofiles"
        r = self.curl("GET", url, enc_creds)

        if r.status_code == 200:
            object_list = json.loads(r.output)
            self.output(
                object_list, verbose_level=4,
            )
            obj_id = 0
            for obj in object_list["os_x_configuration_profiles"]:
                self.output(
                    obj, verbose_level=3,
                )
                # we need to check for a case-insensitive match
                if obj["name"].lower() == object_name.lower():
                    obj_id = obj["id"]
            return obj_id

    def get_api_obj_value_from_id(
        self, jamf_url, object_type, obj_id, obj_path, enc_creds
    ):
        """get the value of an item in a Classic API object"""
        url = f"{jamf_url}/JSSResource/osxconfigurationprofiles/id/{obj_id}"
        r = self.curl("GET", url, enc_creds)

        if r.status_code == 200:
            obj_content = json.loads(r.output)
            self.output("API object content:", verbose_level=2)
            self.output(obj_content, verbose_level=2)

            # for everything else, convert an xpath to json
            xpath_list = obj_path.split("/")
            value = obj_content[object_type]

            for i in range(0, len(xpath_list)):
                if xpath_list[i]:
                    try:
                        value = value[xpath_list[i]]
                        self.output("API object value:", verbose_level=2)
                        self.output(value, verbose_level=2)
                    except KeyError:
                        value = ""
                        break
            if value:
                self.output(f"\nValue of '{obj_path}':\n{value}", verbose_level=2)
            return value

    def substitute_assignable_keys(self, data, cli_custom_keys, xml_escape=False):
        """substitutes any key in the inputted text using the %MY_KEY% nomenclature.
        Whenever %MY_KEY% is found in the provided data, it is replaced with the assigned
        value of MY_KEY. A five-times passa through is done to ensure that all keys are substituted.

        Optionally, if the xml_escape key is set, the value is escaped for XML special characters.
        This is designed primarily to account for ampersands in the substituted strings."""
        loop = 5
        while loop > 0:
            loop = loop - 1
            found_keys = re.findall(r"\%\w+\%", data)
            if not found_keys:
                break
            found_keys = [i.replace("%", "") for i in found_keys]
            for found_key in found_keys:
                if cli_custom_keys[found_key]:
                    self.output(
                        f"Replacing any instances of '{found_key}' with "
                        f"'{str(cli_custom_keys[found_key])}'",
                        verbose_level=2,
                    )
                    if xml_escape:
                        replacement_key = escape(cli_custom_keys[found_key])
                    else:
                        replacement_key = cli_custom_keys[found_key]
                    data = data.replace(f"%{found_key}%", replacement_key)
                else:
                    self.output(f"WARNING: '{found_key}' has no replacement object!",)
        return data

    def pretty_print_xml(self, xml):
        proc = subprocess.Popen(
            ["xmllint", "--format", "/dev/stdin"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        (output, _) = proc.communicate(xml)
        return output

    def get_existing_uuid(self, jamf_url, obj_id, enc_creds):
        """return the existing UUID to ensure we don't change it"""
        # first grab the payload from the xml object
        existing_plist = self.get_api_obj_value_from_id(
            jamf_url,
            "os_x_configuration_profile",
            obj_id,
            "general/payloads",
            enc_creds,
        )

        # Jamf seems to sometimes export an empty key which plistlib considers invalid,
        # so let's remove this
        existing_plist = existing_plist.replace("<key/>", "")

        # make the xml pretty so we can see where the problem importing it is better
        existing_plist = self.pretty_print_xml(bytes(existing_plist, "utf-8"))

        self.output(
            f"Existing payload (type: {type(existing_plist)}):", verbose_level=2
        )
        self.output(existing_plist.decode("UTF-8"), verbose_level=2)

        # now extract the UUID from the existing payload
        existing_payload = plistlib.loads(existing_plist)
        self.output("\nImported payload:", verbose_level=2)
        self.output(existing_payload, verbose_level=2)
        existing_uuid = existing_payload["PayloadUUID"]
        self.output(f"Existing UUID found: {existing_uuid}")
        return existing_uuid

    def generate_uuid(self):
        """generate a UUID for new profiles"""
        return str(uuid.uuid4())

    def make_mobileconfig_from_payload(
        self,
        payload_path,
        payload_identifier,
        mobileconfig_name,
        organization,
        description,
        mobileconfig_uuid,
    ):
        """create a mobileconfig file using a payload file"""
        # import plist and replace any substitutable keys
        with open(payload_path, "rb") as file:
            mcx_preferences = plistlib.load(file)

        self.output("Preferences contents:", verbose_level=2)
        self.output(mcx_preferences, verbose_level=2)

        # generate a random UUID for the payload
        payload_uuid = self.generate_uuid()

        # add the other keys required in the payload
        payload_contents = {
            "PayloadDisplayName": "Custom Settings",
            "PayloadIdentifier": payload_uuid,
            "PayloadOrganization": "JAMF Software",
            "PayloadType": "com.apple.ManagedClient.preferences",
            "PayloadUUID": payload_uuid,
            "PayloadVersion": 1,
            "PayloadContent": {
                payload_identifier: {
                    "Forced": [{"mcx_preference_settings": mcx_preferences}]
                }
            },
        }

        self.output("Payload contents:", verbose_level=2)
        self.output(payload_contents, verbose_level=2)

        # now write the mobileconfig file
        mobileconfig_data = {
            "PayloadDescription": description,
            "PayloadDisplayName": mobileconfig_name,
            "PayloadEnabled": True,
            "PayloadOrganization": organization,
            "PayloadRemovalDisallowed": True,
            "PayloadScope": "System",
            "PayloadType": "Configuration",
            "PayloadVersion": 1,
            "PayloadIdentifier": mobileconfig_uuid,
            "PayloadUUID": mobileconfig_uuid,
            "PayloadContent": [payload_contents],
        }

        self.output("Converting config data to plist")
        mobileconfig_plist = plistlib.dumps(mobileconfig_data)

        self.output("Mobileconfig contents:", verbose_level=2)
        self.output(mobileconfig_plist.decode("UTF-8"), verbose_level=2)

        return mobileconfig_plist

    def unsign_signed_mobileconfig(self, mobileconfig_plist):
        """checks if profile is signed. This is necessary because Jamf cannot
        upload a signed profile, so we either need to unsign it, or bail """
        output_path = os.path.join("/tmp", str(uuid.uuid4()))
        cmd = [
            "/usr/bin/security",
            "cms",
            "-D",
            "-i",
            mobileconfig_plist,
            "-o",
            output_path,
        ]
        self.output(cmd, verbose_level=1)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, err = proc.communicate()
        if os.path.exists(output_path) and os.stat(output_path).st_size > 0:
            self.output(f"Profile is signed. Unsigned profile at {output_path}")
            return output_path
        elif err:
            self.output("Profile is not signed.")
            self.output(err, verbose_level=2)

    def upload_mobileconfig(
        self,
        jamf_url,
        enc_creds,
        mobileconfig_name,
        description,
        category,
        mobileconfig_plist,
        computergroup_name,
        template_contents,
        profile_uuid,
        obj_id=None,
    ):
        """Update Configuration Profile metadata."""

        # if we find an object ID we put, if not, we post
        if obj_id:
            url = f"{jamf_url}/JSSResource/osxconfigurationprofiles/id/{obj_id}"
        else:
            url = f"{jamf_url}/JSSResource/osxconfigurationprofiles/id/0"

        # remove newlines, tabs, leading spaces, and XML-escape the payload
        mobileconfig_plist = mobileconfig_plist.decode("UTF-8")
        mobileconfig_list = mobileconfig_plist.rsplit("\n")
        mobileconfig_list = [x.strip("\t") for x in mobileconfig_list]
        mobileconfig_list = [x.strip(" ") for x in mobileconfig_list]
        mobileconfig = "".join(mobileconfig_list)

        # substitute user-assignable keys
        replaceable_keys = {
            "mobileconfig_name": mobileconfig_name,
            "description": description,
            "category": category,
            "payload": mobileconfig,
            "computergroup_name": computergroup_name,
            "uuid": f"com.github.grahampugh.jamf-upload.{profile_uuid}",
        }

        # for key in replaceable_keys:
        #     self.output(f"TEMP: {replaceable_keys[key]}")

        # substitute user-assignable keys (escaping for XML)
        template_contents = self.substitute_assignable_keys(
            template_contents, replaceable_keys, xml_escape=True
        )

        self.output("Configuration Profile to be uploaded:", verbose_level=2)
        self.output(template_contents, verbose_level=2)

        self.output("Uploading Configuration Profile..")
        # write the template to temp file
        template_xml = self.write_temp_file(template_contents)

        count = 0
        while True:
            count += 1
            self.output(
                f"Configuration Profile upload attempt {count}", verbose_level=1
            )
            method = "PUT" if obj_id else "POST"
            r = self.curl(method, url, enc_creds, template_xml)
            # check HTTP response
            if (
                self.status_check(r, "Configuration Profile", mobileconfig_name)
                == "break"
            ):
                break
            if count > 5:
                self.output(
                    "ERROR: Configuration Profile upload did not succeed after 5 attempts"
                )
                self.output(f"\nHTTP POST Response Code: {r.status_code}")
                break
            sleep(10)

        return r

    def main(self):
        """Do the main thing here"""
        self.jamf_url = self.env.get("JSS_URL")
        self.jamf_user = self.env.get("API_USERNAME")
        self.jamf_password = self.env.get("API_PASSWORD")
        self.profile_name = self.env.get("profile_name")
        self.payload = self.env.get("payload")
        self.mobileconfig = self.env.get("mobileconfig")
        self.identifier = self.env.get("identifier")
        self.template = self.env.get("profile_template")
        self.profile_category = self.env.get("profile_category")
        self.organization = self.env.get("organization")
        self.profile_description = self.env.get("profile_description")
        self.profile_computergroup = self.env.get("profile_computergroup")
        self.unsign = self.env.get("unsign_profile")
        # handle setting unsign in overrides
        if not self.unsign or self.unsign == "False":
            self.replace = False
        self.replace = self.env.get("replace_profile")
        # handle setting replace in overrides
        if not self.replace or self.replace == "False":
            self.replace = False

        # clear any pre-existing summary result
        if "jamfcomputerprofileuploader_summary_result" in self.env:
            del self.env["jamfcomputerprofileuploader_summary_result"]

        profile_updated = False

        # encode the username and password into a basic auth b64 encoded string
        credentials = f"{self.jamf_user}:{self.jamf_password}"
        enc_creds_bytes = b64encode(credentials.encode("utf-8"))
        enc_creds = str(enc_creds_bytes, "utf-8")

        # handle files with no path
        if self.payload and "/" not in self.payload:
            found_payload = self.get_path_to_file(self.payload)
            if found_payload:
                self.payload = found_payload
            else:
                raise ProcessorError(f"ERROR: Payload file {self.payload} not found")
        if self.mobileconfig and "/" not in self.mobileconfig:
            found_mobileconfig = self.get_path_to_file(self.mobileconfig)
            if found_mobileconfig:
                self.mobileconfig = found_mobileconfig
            else:
                raise ProcessorError(
                    f"ERROR: mobileconfig file {self.mobileconfig} not found"
                )
        if self.template and "/" not in self.template:
            found_template = self.get_path_to_file(self.template)
            if found_template:
                self.template = found_template
            else:
                raise ProcessorError(
                    f"ERROR: XML template file {self.template} not found"
                )

        # if an unsigned mobileconfig file is supplied we can get the name, organization and
        # description from it
        if self.mobileconfig:
            self.output(f"mobileconfig file supplied: {self.mobileconfig}")
            # check if the file is signed
            mobileconfig_file = self.unsign_signed_mobileconfig(self.mobileconfig)
            # quit if we get an unsigned profile back and we didn't select --unsign
            if mobileconfig_file and not self.unsign:
                raise ProcessorError(
                    "Signed profiles cannot be uploaded to Jamf Pro via the API. "
                    "Use the GUI to upload the signed profile, or use --unsign to upload "
                    "the profile with the signature removed."
                )

            # import mobileconfig
            with open(self.mobileconfig, "rb") as file:
                mobileconfig_contents = plistlib.load(file)
            with open(self.mobileconfig, "rb") as file:
                mobileconfig_plist = file.read()
            try:
                mobileconfig_name = mobileconfig_contents["PayloadDisplayName"]
                self.output(f"Configuration Profile name: {mobileconfig_name}")
                self.output("Mobileconfig contents:", verbose_level=2)
                self.output(mobileconfig_plist.decode("UTF-8"), verbose_level=2)
            except KeyError:
                raise ProcessorError(
                    "ERROR: Invalid mobileconfig file supplied - cannot import"
                )
            try:
                description = mobileconfig_contents["PayloadDescription"]
            except KeyError:
                description = ""
            try:
                organization = mobileconfig_contents["PayloadOrganization"]
            except KeyError:
                organization = ""

        # otherwise we are dealing with a payload plist and we need a few other bits of info
        else:
            if not self.profile_name:
                raise ProcessorError("ERROR: No profile name supplied - cannot import")
            if not self.payload:
                raise ProcessorError(
                    "ERROR: No path to payload file supplied - cannot import"
                )
            if not self.identifier:
                raise ProcessorError(
                    "ERROR: No identifier for mobileconfig supplied - cannot import"
                )
            mobileconfig_name = self.profile_name
            description = ""
            organization = ""

        # we provide a default template which has no category or scope
        if not self.template:
            self.template = "Jamf_Templates/ProfileTemplate-no-scope.xml"

        # automatically provide a description and organisation from the mobileconfig
        # if not provided in the options
        if not self.profile_description:
            if description:
                self.profile_description = description
            else:
                self.profile_description = (
                    "Config profile created by AutoPkg and JamfComputerProfileUploader"
                )
        if not self.organization:
            if organization:
                self.organization = organization
            else:
                organization = "AutoPkg"
                self.organization = organization

        # import profile template
        with open(self.template, "r") as file:
            template_contents = file.read()

        # check for existing Configuration Profile
        self.output(f"Checking for existing '{mobileconfig_name}' on {self.jamf_url}")
        obj_id = self.get_api_obj_id_from_name(
            self.jamf_url, mobileconfig_name, enc_creds
        )
        if obj_id:
            self.output(
                f"Configuration Profile '{mobileconfig_name}' already exists: ID {obj_id}"
            )
            if self.replace:
                # grab existing UUID from profile as it MUST match on the destination
                existing_uuid = self.get_existing_uuid(self.jamf_url, obj_id, enc_creds)

                if not self.mobileconfig:
                    # generate the mobileconfig from the supplied payload
                    mobileconfig_plist = self.make_mobileconfig_from_payload(
                        self.payload,
                        self.identifier,
                        mobileconfig_name,
                        self.organization,
                        self.profile_description,
                        existing_uuid,
                    )

                # now upload the mobileconfig by generating an XML template
                if mobileconfig_plist:
                    self.upload_mobileconfig(
                        self.jamf_url,
                        enc_creds,
                        mobileconfig_name,
                        self.profile_description,
                        self.profile_category,
                        mobileconfig_plist,
                        self.profile_computergroup,
                        template_contents,
                        existing_uuid,
                        obj_id,
                    )
                    profile_updated = True
                else:
                    self.output("A mobileconfig was not generated so cannot upload.")
            else:
                self.output(
                    "Not replacing existing Configuration Profile. "
                    "Override the replace_profile key to True to enforce."
                )
        else:
            self.output(
                f"Configuration Profile '{mobileconfig_name}' not found - will create"
            )
            new_uuid = self.generate_uuid()

            if not self.mobileconfig:
                # generate the mobileconfig from the supplied payload
                mobileconfig_plist = self.make_mobileconfig_from_payload(
                    self.payload,
                    self.identifier,
                    mobileconfig_name,
                    self.organization,
                    self.profile_description,
                    new_uuid,
                )

            # now upload the mobileconfig by generating an XML template
            if mobileconfig_plist:
                self.upload_mobileconfig(
                    self.jamf_url,
                    enc_creds,
                    mobileconfig_name,
                    self.profile_description,
                    self.profile_category,
                    mobileconfig_plist,
                    self.profile_computergroup,
                    template_contents,
                    new_uuid,
                )
            else:
                raise ProcessorError(
                    "A mobileconfig was not generated so cannot upload."
                )

        # output the summary
        self.env["profile_name"] = self.profile_name
        self.env["profile_updated"] = profile_updated
        if profile_updated:
            self.env["jamfcomputerprofileuploader_summary_result"] = {
                "summary_text": (
                    "The following configuration profiles were uploaded to "
                    "or updated in Jamf Pro:"
                ),
                "report_fields": ["mobileconfig_name", "profile_category"],
                "data": {
                    "mobileconfig_name": mobileconfig_name,
                    "profile_category": self.profile_category,
                },
            }


if __name__ == "__main__":
    PROCESSOR = JamfComputerProfileUploader()
    PROCESSOR.execute_shell()
