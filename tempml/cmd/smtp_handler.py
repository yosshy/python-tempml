
# Copyright 2017 by Akira Yoshiyama <akirayoshiyama@gmail.com>.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
The entry point
"""

import argparse
import asyncore
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import email_normalize
import logging
import os
import pbr.version
import re
import smtpd
import smtplib
import sys

from tempml import const
from tempml import db
from tempml import utils


NEW_ML_ACCOUNT = os.environ.get("TEMPML_NEW_ML_ACCOUNT", "new")
DB_URL = os.environ.get("TEMPML_DB_URL", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("TEMPML_DB_NAME", "tempml")
LISTEN_ADDRESS = os.environ.get("TEMPML_LISTEN_ADDRESS", "127.0.0.1")
LISTEN_PORT = os.environ.get("TEMPML_LISTEN_PORT", 25)
RELAY_HOST = os.environ.get("TEMPML_RELAY_HOST", "localhost")
RELAY_PORT = os.environ.get("TEMPML_RELAY_PORT", 1025)
DOMAIN = os.environ.get("TEMPML_DOMAIN", "localdomain")
ML_NAME_FORMAT = os.environ.get("TEMPML_ML_NAME_FORMAT", "[ml-%06d]")
STATIC_ADDRESS_LIST = os.environ.get("TEMPML_STATIC_ADDRESS_LIST")


def normalize(addresses):
    logging.debug(addresses)
    result = []
    for address in addresses:
        try:
            cleaned = email_normalize.normalize(address, resolve=False)
            if isinstance(cleaned, str):
                result.append(cleaned)
        except:
            pass
    return set(result)


class TempMlSMTPServer(smtpd.SMTPServer):

    def __init__(self, listen_address, listen_port, relay_host, relay_port,
                 db_url, db_name, ml_name_format, new_ml_account, domain,
                 admin_file, verbose):

        self.relay_host = relay_host
        self.relay_port = relay_port
        self.ml_name_format = ml_name_format
        self.new_ml_account = new_ml_account
        self.domain = domain
        self.verbose = verbose
        self.new_ml_address = new_ml_account + "@" + domain
        self.admins = set()
        if admin_file:
            with open(admin_file) as f:
                self.admins = normalize(f.readlines())

        db.init_db(db_url, db_name)

        return super(TempMlSMTPServer, self).__init__(
            (listen_address, listen_port), None)

    def process_message(self, peer, mailfrom, rcpttos, data):
        message = email.message_from_string(data)
        if not message.is_multipart():
            _message = MIMEMultipart()
            for header, value in message.items():
                _message[header] = value
            _message.attach(MIMEText(message.get_payload()))
            message = _message

        from_str = message.get('from', "").strip()
        to_str = message.get('to', "").strip()
        cc_str = message.get('cc', "").strip()
        logging.debug("From: %s", from_str)
        logging.debug("To: %s", to_str)
        logging.debug("Cc: %s", cc_str)

        _from = normalize([from_str])
        to = normalize(to_str.split(','))
        cc = normalize(cc_str.split(','))
        logging.warning("To: %s", to)
        logging.warning("Cc: %s", cc)
        logging.warning("From: %s", _from)

        # checking cross-post
        mls = [_ for _ in (to | cc) if _.endswith("@" + self.domain)]
        if len(mls) == 0:
            logging.error("No ML specified")
            return const.SMTP_STATUS_NO_ML_SPECIFIED
        elif len(mls) > 1:
            logging.error("Can't cross-post a message")
            return const.SMTP_STATUS_CANT_CROSS_POST

        # Aquire the ML name
        ml_address = mls[0]
        ml_name = ml_address.replace("@" + self.domain, "")

        # Remove the ML name from to'd and cc'd address lists
        if ml_address in to:
            to.remove(ml_address)
        if ml_address in cc:
            cc.remove(ml_address)

        # Want a new ML?
        if ml_name == self.new_ml_account:
            ml_name = self.ml_name_format % db.increase_counter()
            db.create_ml(ml_name, (to | cc | _from) - self.admins)
            self.send_post(ml_name, message)
            return

        # Post a message to an existing ML
        # Checking members
        members = db.get_members(ml_name)
        if members is None:
            logging.error("No such ML")
            return const.SMTP_STATUS_NO_SUCH_ML

        # Checking whether the sender is one of the ML members
        if mailfrom not in (members | self.admins):
            logging.error("Non-member post")
            return const.SMTP_STATUS_NOT_MEMBER

        # Remove cc'd members from the ML members if the subject is empty
        if message.get('subject', "") == "":
            if len(cc - self.admins) > 0:
                db.del_members(ml_name, (cc - self.admins))
            return

        # Checking To: and Cc:
        if len(cc - self.admins) > 0:
            db.add_members(ml_name, (cc - self.admins))

        # Send a post to the members of the ML
        self.send_post(ml_name, message)

    def send_post(self, ml_name, message):
        """
        Send a post to the ML members

        :param ml_name: ML name
        :type ml_name: str
        :param message: MIME multipart object
        :type message: email.mime.multipart.MIMEMultipart
        :rtype: None
        """
        logging.debug("ml_name: %s", ml_name)

        # Format the post
        _from = ml_name + "@" + self.domain
        message.replace_header('to',  _from)
        if 'reply-to' in message:
            message.replace_header('reply-to', _from)
        else:
            message['reply-to'] = _from
        subject = message['subject']
        subject = re.sub(r"^(Re:|re:)\s*\[%s\]\s*" % ml_name, "", subject)
        message.replace_header('Subject', "[%s] %s" % (
                               ml_name, message['subject']))

        # Send a post to the relay host
        members = db.get_members(ml_name) | self.admins
        relay = smtplib.SMTP(self.relay_host, self.relay_port)
        relay.set_debuglevel(1)
        relay.sendmail(_from, members, message.as_string())
        relay.quit()


def main(**kwargs):
    """
    The main routine
    """
    if version:
        print(pbr.version.VersionInfo('tempml'))
        return 0

    server = TempMlSMTPServer(**kwargs)
    asyncore.loop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version',
                        help='Print version and exit',
                        action='store_true')
    parser.add_argument('--verbose',
                        help='Verbose output',
                        action='store_true')
    parser.add_argument('--new-ml-account',
                        help='Account to create new ml',
                        default=NEW_ML_ACCOUNT)
    parser.add_argument('--db-url',
                        help='Database URL',
                        default=DB_URL)
    parser.add_argument('--db-name',
                        help='Database name',
                        default=DB_NAME)
    parser.add_argument('--listen-address',
                        help='Listen address',
                        default=LISTEN_ADDRESS)
    parser.add_argument('--listen-port', type=int,
                        help='Listen port',
                        default=LISTEN_PORT)
    parser.add_argument('--relay-host',
                        help='SMTP server to relay',
                        default=RELAY_HOST)
    parser.add_argument('--relay-port', type=int,
                        help='SMTP server port to relay',
                        default=RELAY_PORT)
    parser.add_argument('--domain',
                        help='Domain name for email',
                        default=DOMAIN)
    parser.add_argument('--ml-name-format',
                        help='ML name format string',
                        default=ML_NAME_FORMAT)
    parser.add_argument('--static-address-list',
                        help='filename within email address list',
                        default=STATIC_ADDRESS_LIST)

    opts = parser.parse_args()
    sys.exit(main(**opts.__dict__))