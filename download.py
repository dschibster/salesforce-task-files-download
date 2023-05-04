import concurrent.futures
from simple_salesforce import Salesforce
import requests
import os
import csv
import re
import logging
import threading

task_available = threading.Event()
task_ids = None

def split_into_batches(items, batch_size):
    full_list = list(items)
    for i in range(0, len(full_list), batch_size):
        yield full_list[i:i + batch_size]


def create_filename(title, file_extension, content_document_id, output_directory):
    # Create filename
    if ( os.name == 'nt') :
        # on windows, this is harder 
        # see https://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename

        bad_chars= re.compile(r'[^A-Za-z0-9_. ]+|^\.|\.$|^ | $|^$')
        bad_names= re.compile(r'(aux|com[1-9]|con|lpt[1-9]|prn)(\.|$)')
        clean_title = bad_chars.sub('_', title)
        if bad_names.match(clean_title) :
            clean_title = '_'+clean_title

    else :

        bad_chars = [';', ':', '!', "*", '/', '\\']
        clean_title = filter(lambda i: i not in bad_chars, title)
        clean_title = ''.join(list(clean_title))

    filename = "{0}{1} {2}.{3}".format(output_directory, content_document_id, clean_title, file_extension)
    return filename


def get_task_ids(sf, query):
    global task_ids
    task_ids = set()
    tasks = sf.query_all(query)
    logging.debug("Queried Tasks: {0}".format(query))
    for task in tasks["records"]:
        task_ids.add(task["Id"])
    
    task_available.set()

def get_content_document_ids(sf, output_directory, query):
    # Locate/Create output directory
    if not os.path.isdir(output_directory):
        os.mkdir(output_directory)

    results_path = output_directory + 'files.csv'
    content_document_ids = set()
    content_documents = sf.query_all(query)

    # Save results file with file mapping and return ids
    writeheaderrow = False
    if not os.path.isfile(results_path):
        writeheaderrow = True;


    with open(results_path, 'a', encoding='UTF-8', newline='') as results_csv:
        filewriter = csv.writer(results_csv, delimiter=',', quotechar='|', quoting=csv.QUOTE_MINIMAL)
        
        if writeheaderrow:
            filewriter.writerow(['FirstPublicationId','FirstPublicationName', 'ContentDocumentId', 'Title','VersionData','PathOnClient'])


        for content_document in content_documents["records"]:
            content_document_ids.add(content_document["ContentDocumentId"])
            filename = create_filename(content_document["ContentDocument"]["Title"],
                                       content_document["ContentDocument"]["FileExtension"],
                                       content_document["ContentDocumentId"],
                                       output_directory)

            filewriter.writerow(
                [content_document["LinkedEntityId"], content_document["LinkedEntity"]["Name"], content_document["ContentDocumentId"],
                 content_document["ContentDocument"]["Title"], filename, filename])

    return content_document_ids


def download_file(args):
    record, output_directory, sf = args
    filename = create_filename(record["Title"], record["FileExtension"], record["ContentDocumentId"], output_directory)
    url = "https://%s%s" % (sf.sf_instance, record["VersionData"])

    logging.debug("Downloading from " + url)
    response = requests.get(url, headers={"Authorization": "OAuth " + sf.session_id,
                                          "Content-Type": "application/octet-stream"})

    if response.ok:
        # Save File
        with open(filename, "wb") as output_file:
            output_file.write(response.content)
        return "Saved file to %s" % filename
    else:
        return "Couldn't download %s" % url


def fetch_files(sf, query_string, output_directory, valid_content_document_ids=None, batch_size=100):
    # Divide the full list of files into batches of 100 ids
    batches = list(split_into_batches(valid_content_document_ids, batch_size))

    i = 0
    for batch in batches:

        i = i + 1
        logging.info("Processing batch {0}/{1}".format(i, len(batches)))
        batch_query = query_string + ' AND ContentDocumentId in (' + ",".join("'" + item + "'" for item in batch) + ')'
        query_response = sf.query(batch_query)
        records_to_process = len(query_response["records"])
        logging.debug("Content Version Query found {0} results".format(records_to_process))

        while query_response:
            with concurrent.futures.ProcessPoolExecutor() as executor:
                args = ((record, output_directory, sf) for record in query_response["records"])
                for result in executor.map(download_file, args):
                    logging.debug(result)
            break

        logging.debug('All files in batch {0} downloaded'.format(i))
    logging.debug('All batches complete')


def main():
    import argparse
    import configparser

    parser = argparse.ArgumentParser(description='Export ContentVersion (Files) from Salesforce')
    parser.add_argument('-q', '--query', metavar='query', required=True,
                        help='SOQL to limit the valid ContentDocumentIds. Must return the Ids of parent objects.')
    args = parser.parse_args()

    # Get settings from config file
    config = configparser.ConfigParser(allow_no_value=True)
    config.read('download.ini')

    username = config['salesforce']['username']
    password = config['salesforce']['password']
    token = config['salesforce']['security_token']

    domain = config['salesforce']['domain']
    if domain :
        domain += '.my'
    else :
        domain = 'login'
    
    batch_size = int(config['salesforce']['batch_size'])
    is_sandbox = config['salesforce']['connect_to_sandbox']
    loglevel = logging.getLevelName(config['salesforce']['loglevel'])
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=loglevel)

    

    subquery = ""
    i = 0;
    # Connect
    if is_sandbox == 'True':
        domain = 'test'
    sf = Salesforce(username=username, password=password, security_token=token, domain=domain)
    logging.debug("Connected successfully to {0}".format(sf.sf_instance))
    logging.info("SELECTED QUERY: {0}".format(args.query))

    thread = threading.Thread(target=get_task_ids(sf=sf, query=args.query))
    thread.start()

    # wait here for the result to be available before continuing
    task_available.wait()
    
    for task_id in task_ids:

        subquery = subquery + "'"+task_id+"',"
        i += 1

        if i == 200:              
            subquery = subquery[:-1]
            content_document_query = 'SELECT ContentDocumentId, LinkedEntityId, LinkedEntity.Name, ContentDocument.Title, ' \
                                    'ContentDocument.FileExtension FROM ContentDocumentLink ' \
                                    'WHERE LinkedEntityId in ({0})'.format(subquery)
            output = config['salesforce']['output_dir']
            query = "SELECT ContentDocumentId, Title, VersionData, FileExtension FROM ContentVersion " \
                    "WHERE IsLatest = True AND FileExtension != 'snote'"

            if is_sandbox == 'True':
                domain = 'test'

            # Output
            logging.info('Export ContentVersion (Files) from Salesforce')
            logging.info('Username: ' + username)
            logging.info('Signing in at: https://'+ domain + '.salesforce.com')
            logging.info('Output directory: ' + output)

            
            # Get Content Document Ids
            logging.debug("Querying to get Content Document Ids...")
            valid_content_document_ids = None
            if content_document_query:
                valid_content_document_ids = get_content_document_ids(sf=sf, output_directory=output,
                                                                    query=content_document_query)
            logging.info("Found {0} total files".format(len(valid_content_document_ids)))

            # Begin Downloads
            fetch_files(sf=sf, query_string=query, valid_content_document_ids=valid_content_document_ids,
                        output_directory=output, batch_size=batch_size)

            subquery = ""
            i = 0

    subquery = subquery[:-1]
    content_document_query = 'SELECT ContentDocumentId, LinkedEntityId, LinkedEntity.Name, ContentDocument.Title, ' \
                                    'ContentDocument.FileExtension FROM ContentDocumentLink ' \
                                    'WHERE LinkedEntityId in ({0})'.format(subquery)
    output = config['salesforce']['output_dir']
    query = "SELECT ContentDocumentId, Title, VersionData, FileExtension FROM ContentVersion " \
            "WHERE IsLatest = True AND FileExtension != 'snote'"

    if is_sandbox == 'True':
        domain = 'test'

    # Output
    logging.info('Export ContentVersion (Files) from Salesforce')
    logging.info('Username: ' + username)
    logging.info('Signing in at: https://'+ domain + '.salesforce.com')
    logging.info('Output directory: ' + output)

    # Connect
    sf = Salesforce(username=username, password=password, security_token=token, domain=domain)
    logging.debug("Connected successfully to {0}".format(sf.sf_instance))

    # Get Content Document Ids
    logging.debug("Querying to get Content Document Ids...")
    valid_content_document_ids = None
    if content_document_query:
        valid_content_document_ids = get_content_document_ids(sf=sf, output_directory=output,
                                                            query=content_document_query)
    logging.info("Found {0} total files".format(len(valid_content_document_ids)))

    # Begin Downloads
    fetch_files(sf=sf, query_string=query, valid_content_document_ids=valid_content_document_ids,
                output_directory=output, batch_size=batch_size)
    
if __name__ == "__main__":
    main()
