"""
BitBake nuget fetcher implementation

SRC_URI = "nuget://sourcename/package/version;OptionA=xxx;OptionB=xxx;..."

Supported SRC_URI options are:

- downloadfilename
    Specifies the filename used when storing the downloaded file.

This fetcher uses the nuget v3 API's SearchQueryService to look up a package
id/version; this does require a few extra URL hops to get us to a download URL.

The nuget v3 API does have a PackageBaseAddress service that would be a more
direct path to a download URL, but unfortunately, Artifactory does not support
it, so we have to do the search route.
See https://github.com/dependabot/dependabot-core/issues/8887#issuecomment-1935082966
where they also ran into this.
"""
# Copyright (C) 2025 Emerson
#
# SPDX-License-Identifier: GPL-2.0-only

import base64
import json
import os
import re
import tempfile
import bb
import urllib
import zipfile
from bb.fetch2 import Fetch
from bb.fetch2 import FetchError
from bb.fetch2 import FetchMethod
from bb.fetch2 import MissingParameterError
from bb.fetch2 import ParameterError
from bb.fetch2 import runfetchcmd
from bb.fetch2.wget import Wget

class Nuget(Wget):
    """Class to fetch a package from a nuget repository"""

    KNOWN_SOURCES = {
        'nuget.org': 'https://api.nuget.org/v3/index.json',
    }

    def __init__(self, *args, **kwargs):
        super(Nuget, self).__init__(*args, **kwargs)

        self.search_query_sources = {}

    def _service_index_url(self, ud, d):
        index_url = d.getVar("NUGET_SOURCE_%s" % ud.sourcename)
        if not index_url and ud.sourcename in self.KNOWN_SOURCES:
            index_url = self.KNOWN_SOURCES[ud.sourcename]
        return index_url

    def _fetch_resource(self, ud, d, uri, suffix="resource"):
        bb.debug(1, "Fetching %s content from %s" % (suffix, uri))
        f = tempfile.NamedTemporaryFile()
        with tempfile.TemporaryDirectory(prefix="nuget-%s-" % suffix) as workdir, tempfile.NamedTemporaryFile(dir=workdir, prefix="nuget-%s" % suffix) as f:
            fetchcmd = self.basecmd
            fetchcmd += " -O " + f.name + " --user-agent='" + self.user_agent + "' '" + uri + "'"
            try:
                self._runwget(ud, d, fetchcmd, True, workdir=workdir)
                fetchresult = f.read()
            except bb.fetch2.BBFetchException:
                feetchresult = ""

        return fetchresult

    def _get_search_query_service_url(self, ud, d, sourcename):
        if sourcename in self.search_query_sources:
            return self.search_query_sources[sourcename]

        service_index_url = self._service_index_url(ud, d)
        service_index = json.loads(self._fetch_resource(ud, d, service_index_url, suffix="service-index"))

        # parse the service index:
        # https://learn.microsoft.com/en-us/nuget/api/service-index
        if not 'version' in service_index:
            raise FetchError("Service index json doesn't have a version key; Is this a nuget repository?", service_index_url)
        if not 'resources' in service_index:
            raise FetchError("Service index json doesn't have a resources key; Is this a nuget repository?", service_index_url)
        for rsrc in service_index['resources']:
            if not '@type' in rsrc:
                raise FetchError("Service resource is missing @type", service_index_url)
            if not '@id' in rsrc:
                raise FetchError("Service resource is missing @id", service_index_url)
            if rsrc['@type'].startswith("SearchQueryService/"):
                self.search_query_service = rsrc['@id']
                return rsrc['@id']
        raise FetchError("Unable to find SearchQueryResource in service index.", service_index_url)

    def _get_package_download_url(self, ud, d, sourcename, package, version):
        search_query_service = self._get_search_query_service_url(ud, d, sourcename)
        # Build a search query to find our package by id/version
        # https://learn.microsoft.com/en-us/nuget/api/search-query-service-resource
        search_query_url = (self.search_query_service + '?q=' +
            urllib.parse.quote('id:' + package + ' version:' + version) +
            '&semVerLevel=2.0.0&prerelease=true')
        listing_data = json.loads(self._fetch_resource(ud, d, search_query_url, 'package-query'))

        # we should really have only one result and one version
        package_metadata_url = None
        for result in listing_data['data']:
            if result['id'] == package:
                bb.debug(1, "found package %s in metadata" % package)
                for ver in result['versions']:
                    if ver['version'] == version:
                        package_metadata_url = ver['@id']
                        break;

        if package_metadata_url is None:
            raise FetchError("Unable to find package %s version %s" %  (package, version), search_query_url)

        version_listing_data = json.loads(self._fetch_resource(ud, d, package_metadata_url, 'package-md-query'))
        download_url = version_listing_data['packageContent']

        bb.debug(1, "packageContent URL for package: %s" % download_url)

        return download_url

    def supports(self, ud, d):
        """Check if a given url can be fetched with nuget"""
        return ud.type in ["nuget"]

    def urldata_init(self, ud, d):
        """Init nuget specific variables within url data"""
        super(Nuget, self).urldata_init(ud, d)

        # URL syntax is: nuget://[user@pass:]SOURCENAME/PACKAGE/VERSION;options
        # superclass urldata_init has already done decodeurl() for some of this but
        # we need to break up the path.
        ud.sourcename = ud.host;
        parts = ud.path.split('/')

        if len(parts) < 3:
            raise bb.fetch2.ParameterError("Invalid URL: Must be nuget://SOURCENAME/PACKAGE/VERSION", ud.url)
        ud.package = parts[1]
        ud.version = parts[2]

        if not self._service_index_url(ud, d):
            raise bb.fetch2.ParameterError("Unknown nuget source '%s'; set NUGET_SOURCE_%s to the service index URL." % (ud.sourcename, ud.sourcename), ud.url)

        # superclass urldata_init set default basename based on path, we need to reset it
        if 'downloadfilename' in ud.parm:
            ud.basename = ud.parm['downloadfilename']
        else:
            ud.basename = "%s-%s.nupkg" % (ud.package, ud.version)

        ud.localfile = d.expand(urllib.parse.unquote(ud.basename))

    def download(self, ud, d):
        """Fetch urls"""

        ud.url = self._get_package_download_url(ud, d, ud.sourcename, ud.package, ud.version)

        try:
            super(Nuget, self).download(ud, d)
        except bb.fetch2.BBFetchException:
            if "+" in ud.version:
                # If have buildinfo in your version, Artifactory can end up returning a packageContent
                # URL that only has the base portion in it, and that packageContent URL will 404.
                # If we run into this case, manually try to stick the full build string in there.
                base_version, build_suffix = ud.version.split('+')
                newurl = ud.url.replace(base_version, urllib.parse.quote(ud.version))
                if newurl == ud.url:
                    # This transformation didn't change the URL, so don't try again.
                    raise
                bb.debug(1, "Expanding version in URL: %s" % newurl)
                ud.url = newurl
                super(Nuget, self).download(ud, d)
            else:
                raise

    def unpack(self, urldata, rootdir, data):
        try:
            unpack = bb.utils.to_boolean(urldata.parm.get('unpack'), True)
        except ValueError as exc:
            bb.fatal("Invalid value for 'unpack' parameter for %s: %s" %
                     (file, urldata.parm.get('unpack')))

        # If 'subdir' param exists, create a dir and use it as destination for unpack cmd
        if 'subdir' in urldata.parm:
            subdir = urldata.parm.get('subdir')
            if os.path.isabs(subdir):
                if not os.path.realpath(subdir).startswith(os.path.realpath(rootdir)):
                    raise UnpackError("subdir argument isn't a subdirectory of unpack root %s" % rootdir, urldata.url)
                unpackdir = subdir
            else:
                unpackdir = os.path.join(rootdir, subdir)
        else:
            unpackdir = os.path.join(rootdir, "nuget")

        bb.utils.mkdirhier(unpackdir)

        with zipfile.ZipFile(urldata.localpath, mode='r') as zip:
            zip.extractall(unpackdir)
