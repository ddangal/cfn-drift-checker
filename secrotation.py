import boto3
import os
import pg
import pgdb
import json
from botocore.exceptions import ClientError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

def lambda_handler():
    os.chdir('/tmp')
    secret_client = boto3.client('secretsmanager')
    db_client = boto3.client('rds')
    ses_client= boto3.client('ses')
    clusters = db_client.describe_db_clusters()
    for cluster in clusters['DBClusters']:
        clusterid = cluster['DBClusterIdentifier']
        clustersecret = ('rotationtest/' + clusterid)
        masterid = (clusterid + 'AuroraUserSecret')
        secrets = secret_client.list_secrets()
        exclude_characters = os.environ['EXCLUDE_CHARACTERS'] if 'EXCLUDE_CHARACTERS' in os.environ else ':/@"\'\\'
        try:
            for secret in secrets['SecretList']:
                if clustersecret in secret['Name'] and 'postgres' in secret.SecretString['engine']:
                    csecretname = secret['Name']
                    try:
                        currentsecret = secret_client.get_secret_value(SecretId=csecretname)
                        current_dict = currentsecret['SecretString']
                        try:
                            previous_dict = secret_client.get_secret_value(SecretId=csecretname, VersionStage='AWSPREVIOUS')
                        except (secret_client.exceptions.ResourceNotFoundException, KeyError):
                            previous_dict = None
                        pending_dict = current_dict
                        newpw = secret_client.get_random_password(ExcludeCharacters=exclude_characters)
                        pending_dict['password'] = newpw['RandomPassword']
                        secret_client.put_secret_value(SecredId=csecretname, SecretString=json.dumps(current_dict), VersionStages=['AWSPENDING'])
                        pendingversionid = secret_client.get_secret_value(SecretId=csecretname, VersionStage='AWSPENDING')['VersionId']
                        conn = get_connection(current_dict)
                        if not conn:
                            if previous_dict:
                                conn = get_connection(previous_dict)
                            else:
                                for mastersecret in secrets['SecretList']:
                                    if masterid in mastersecret['Name']:
                                        msecretname = secret['Name']
                                        try:
                                            master_dict = secret_client.get_secret_value(SecretId=msecretname)[
                                                'SecretString']
                                            conn = get_connection(master_dict)
                                        except ClientError as e:
                                            print('No Valid Credentials', e)
                                            su = open("rotationstatus.csv", "a+")
                                            su.write(
                                                "Secret " + csecretname +
                                                " Status: No Valid Credentials " +
                                                e +
                                                "\n"
                                            )
                                            su.close()
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SELECT quote_ident(%s)", (pending_dict['username'],))
                                escaped_username = cur.fetchone()[0]

                                alter_role = "ALTER USER %s" % escaped_username
                                cur.execute(alter_role + " WITH PASSWORD %s", (pending_dict['password'],))
                                conn.commit()
                        finally:
                            conn.close()
                        try:
                            conn = get_connection(pending_dict)
                            if conn:
                                secret_client.update_secret_version_stage(SecretId=csecretname, VersionStage='AWSCURRENT', MoveToVersionId=pendingversionid)
                                conn.close()
                                s1 = 'Secret Successfully Rotated'
                                print(s1)
                                su = open("rotationstatus.csv", "a+")
                                su.write("Secret " + csecretname + " Status: " + s1 + "\n")
                                su.close()
                            else:
                                conn = get_connection(master_dict)
                                try:
                                    with conn.cursor() as cur:
                                        cur.execute("SELECT quote_ident(%s)", (pending_dict['username'],))
                                        escaped_username = cur.fetchone()[0]
                                        alter_role = "ALTER USER %s" % escaped_username
                                        cur.execute(alter_role + " WITH PASSWORD %s", (current_dict['password'],))
                                        conn.commit()
                                except ClientError as e:
                                    print('Unable to Roll Back. Manual Intervention Required', e)
                                    su = open("rotationstatus.csv", "a+")
                                    su.write("Secret " + csecretname + " Status: Rollback Failed " + e + "\n")
                                    su.close()
                        except ClientError as e:
                            print('Validation Error, Please Check Credentials', e)
                            su = open("rotationstatus.csv", "a+")
                            su.write(
                                "Secret " + csecretname +
                                " Status: Validation Error, Please Validate " +
                                e +
                                "\n"
                            )
                            su.close()
                    except ClientError as e:
                        print('Unable to Set New Password', e)
                        su = open("rotationstatus.csv", "a")
                        su.write("Secret " + csecretname + " Status: Unable to Set New Password " + e + "\n")
                        su.close()
        except ClientError as e:
            print('Error', e)
    try:
        env = os.environ(env)
        sender = 'aws-operations@aamc.org'
        recipient = os.environ(recipient)
        subject = 'DB Secret Rotation Status' + env
        attachment = '/tmp/rotationstatus.csv'
        body_html = """\
        <html>
        <head></head>
        <body>
        <table align="center" width="600" border="0">
        <tr>
        <td>
        <img src="https://image.email.aamc.org/lib/fe8e13727c63047f73/m/1/a0da6b84-83f5-415a-b7bd-afd168125308.jpg" width="600" height="125" border="0"/>
        </td>
        </tr>
        <tr>
        <td>
        Please see attached Status Report for Database Credential Rotation
        <br/><br/>Sincerely,<br/>Cloud Engineering Team<br/>Association of American Medical Colleges<br/>655 K Street, NW, Suite 100<br>Washington, DC 20001-2399<br/>Email : ccoe@aamc.org<br/><a href="https://www.aamc.org">www.aamc.org</a><br/>Tomorrow's Doctors, Tomorrow's Cures®
        </td>
        </tr>
        </table>
        </body>
        </html>
        """
        charset = "utf-8"
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = recipient
        msg_body = MIMEMultipart('alternative')
        htmlpart = MIMEText(body_html.encode(charset), 'html', charset)
        msg_body.attach(htmlpart)
        att = MIMEApplication(open(attachment, 'rb').read())
        att.add_header('Content-Disposition', 'attachment', filename=os.path.basename(attachment))
        msg.attach(msg_body)
        ses_client.send_raw_email(
            Source=sender,
            Destinations=[recipient],
            RawMessage={
                'Data': msg.as_string()
            }
        )
    except ClientError as e:
        print('Error', e)

def get_connection(secret_dict):
    port = int(secret_dict['port']) if 'port' in secret_dict else 5432
    dbname = secret_dict['dbname'] if 'dbname' in secret_dict else "postgres"
    try:
        conn = pgdb.connect(host=secret_dict['host'], user=secret_dict['username'], password=secret_dict['password'], database=dbname, port=port, connect_timeout=5)
        return conn
    except pg.InternalError:
        return None

