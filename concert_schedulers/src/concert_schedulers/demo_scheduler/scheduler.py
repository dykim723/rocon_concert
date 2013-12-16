#
# License: BSD
#
#   https://raw.github.com/robotics-in-concert/rocon_concert/license/LICENSE
#
##############################################################################
# Imports
##############################################################################

import rospy
import threading
import copy
import unique_id
import concert_msgs.msg as concert_msgs
import concert_msgs.srv as concert_srvs
import scheduler_msgs.msg as scheduler_msgs
import rocon_app_manager_msgs.srv as rapp_manager_srvs
import rocon_scheduler_requests

import demo_allocator
from concert_schedulers.common.exceptions import FailedToStartAppsException

##############################################################################
# Classes
##############################################################################


class DemoScheduler(object):

    POSTFIX_START_APP = '/start_app'
    POSTFIX_STOP_APP = '/stop_app'

    def __init__(self, requests_topic_name):
        self._subscribers = {}       # ros subscribers
        self._services = {}          # ros services
        self._requests = {}          # requester uuid : scheduler_msgs/Request[] - keeps track of all current requests
        self._clients = {}           # concert_msgs/ConcertClient.name : concert_msgs/ConcertClient of all concert clients
        self._inuse = {}             #
        self._pairs = {}             # scheduler_msgs/Resource, concert_msgs/ConcertClient pairs
        self._shutting_down = False  # Used to protect self._pairs when shutting down.
        self._setup_ros_api(requests_topic_name)
        self._lock = threading.Lock()

    def _setup_ros_api(self, requests_topic_name):
        self._subscribers['list_concert_clients'] = rospy.Subscriber(concert_msgs.Strings.CONCERT_CLIENT_CHANGES, concert_msgs.ConcertClients, self._ros_service_list_concert_clients)
        self._subscribers['requests'] = rospy.Subscriber(requests_topic_name, scheduler_msgs.SchedulerRequests, self._process_requests)
        self._services['resource_status'] = rospy.Service('~resource_status', concert_srvs.ResourceStatus, self._ros_service_resource_status)

    def _ros_service_list_concert_clients(self, msg):
        """
            @param
                msg : concert_msgs.ConcertClients

            1. Stops services which client left.
            2. Rebuild client list
            3. Starts services which has all requested resources
        """
        self._lock.acquire()
        clients = msg.clients
        self._stop_services_of_left_clients(clients)

        self._clients = {}
        self.loginfo("clients:")
        for c in clients:
            self.loginfo("  %s" % c.name)
            self._clients[c.name] = c

        self._update()
        self._lock.release()

    def _ros_service_resource_status(self, req):
        """
            respond resource_status request
        """
        resp = concert_srvs.ResourceStatusResponse()
        self._lock.acquire()
        inuse_gateways = self._get_inuse_gateways()
        available = [c for c in self._clients if not self._clients[c].gateway_name in inuse_gateways]

        inuse = []
        for client_node_dict in self._inuse.values():
            for client_node, unused_remapping, max_share, count in client_node_dict.values():
                s = client_node.name + " - " + str(count) + "(" + str(max_share) + ")"
                inuse.append(s)

        resp.available_clients = available
        resp.inuse_clients = inuse

        resp.requested_resources = []
        for s in self._requests:
            srp = concert_msgs.ServiceResourcePair()
            srp.request_id = s
            srp.resources = []
            for request in self._requests[s]:
                for resource in request.resources:
                    srp.resources.append(resource.platform_info + "." + resource.name)
            resp.requested_resources.append(srp)

        resp.engaged_pairs = []
        for s in self._pairs:
            srp = concert_msgs.ServiceResourcePair()
            srp.request_id = s

            for pair in self._pairs[s]:
                resource, client_node = pair
                p = resource.platform_info + " - " + client_node.name + " - " + resource.name
                srp.resources.append(p)
            resp.engaged_pairs.append(srp)
        self._lock.release()
        return resp

    def _process_requests(self, msg):
        """
            1. enable : true. add service or update linkgraph
            2. enable : false. stop service. and remove service
            3. Starts services which has all requested resources

            TODO: Current implementation expects service_name to be unique.
            TODO: it does not rearrange clients if only linkgraph get changed.

            @param : msg
            @type scheduler_msgs.SchedulerRequests
        """
        if len(msg.requests) == 0:
            return  # nothing to do
        # We make some assumptions here - our simple requester either sends
        # all new, or all releasing requests.
        update = False
        self._lock.acquire()
        releasing_requests = []
        requester_id = unique_id.toHexString(msg.requester)
        for request in msg.requests:  # request is scheduler_msgs.Request
            if request.status == scheduler_msgs.Request.NEW:
                if requester_id not in self._requests.keys():
                    self._requests[requester_id] = []
                if request.id not in [r.id for r in self._requests[requester_id]]:
                    self._requests[requester_id].append(request)
                    update = True
            elif request.status == scheduler_msgs.Request.RELEASING:
                releasing_requests.append(unique_id.toHexString(request.id))
            else:
                rospy.logerr("Scheduler : received invalid requests [illegitimate status]")
        for request_id in releasing_requests:
            self._stop_request(request_id)
        if update:
            self._update()
        self._lock.release()

    def _stop_services_of_left_clients(self, clients):
        """
            1. Get gateway_name of clients which left concert
            2. Check all pairs of services whether it is still valid.
            3. Stops services which left_clients are involved

            @param clients : list of client who recently left. list of concert_msgs.ConcertClient
            @type concert_msgs.ConcertClient[]
        """

        left_client_gateways = self._get_left_clients(clients)
        request_to_stop = []

        for key in self._pairs:  # request id key
            if not self._is_service_still_valid(self._pairs[key], left_client_gateways):
                request_to_stop.append(key)

        for key in request_to_stop:
            self._stop_request(key, left_client_gateways)

    def _is_service_still_valid(self, pairs, left_clients):
        """
            @param pair: list of pair (service node, client node)
            @type [(concert_msgs.LinkNode, concert_msgs.ConcertClient)]

            @param left_clients: list of concert client gateways who recently left
            @type string[]
        """
        g_set = set([client_node.gateway_name for _1, client_node in pairs])
        l_set = set(left_clients)

        intersect = g_set.intersection(l_set)
        if len(intersect) > 0:
            return False
        else:
            return True

    def _stop_request(self, request_id, left_client_gateways=[]):
        """
            Stops all clients involved in serving <request_id>

            @param request_id : uuid hex string representing the request id (not the requester id).
            @type hex string

            Note, must be called from within the protection of locked code.
        """
        for p in self._pairs[request_id]:
            resource, client_node = p

            (unused_platform_part, unused_separator, resource_platform_name) = resource.platform_info.rpartition('.')
            service_app_name = resource.name

            is_need_to_stop = self._unmark_client_as_inuse(client_node.gateway_name)

            if not client_node.gateway_name in left_client_gateways:
                if is_need_to_stop:  # only if nobody is using the client
                    # Creates stop app service
                    stop_app_srv = self._get_stop_client_app_service(client_node.gateway_name)

                    # Create stop app request object
                    req = rapp_manager_srvs.StopAppRequest()

                    # Request
                    self.loginfo(" stopping client app [%s][%s][%s]" % (str(resource_platform_name), str(service_app_name), str(client_node.name)))
                    resp = stop_app_srv(req)

                    if not resp.stopped:
                        message = "Failed to stop[" + str(service_app_name) + "] in [" + str(client_node.name) + "]"
                        raise Exception(message)
            else:
                self.loginfo(str(client_node.name) + " has already left")

        del self._pairs[request_id]
        for requester_id in self._requests.keys():
            self._requests[requester_id][:] = [request for request in self._requests[requester_id] if request_id != unique_id.toHexString(request.id)]
            if not self._requests[requester_id]:
                del self._requests[requester_id]
        self.loginfo(str(request_id) + " has been stopped")

    def _get_stop_client_app_service(self, gatewayname):
        """
            @param gateway_name: The client gateway name
            @type string

            @return srv: ROS service instance to stop app
            @type rospy.ServiceProxy
        """
        srv_name = '/' + gatewayname + DemoScheduler.POSTFIX_STOP_APP
        rospy.wait_for_service(srv_name)

        srv = rospy.ServiceProxy(srv_name, rapp_manager_srvs.StopApp)

        return srv

    def _get_left_clients(self, clients):
        """
            @param clients: list of concert client
            @type concert_msgs.ConcertClient[]

            @return left_clients: list of concert client gateway_name who recently left
            @rtype list of string
        """
        inuse_gateways = self._get_inuse_gateways()
        inuse_gateways_set = set(inuse_gateways)
        clients_set = set([c.gateway_name for c in clients])
        left_clients = inuse_gateways_set.difference(clients_set)

        return list(left_clients)

    def _cancel(self, requester_uuid):
        '''
            Note, must be called from within the protection of locked code.

            Deallocate resources (stop apps).
        '''

    def _update(self):
        """
            Brings up requests for resources which can start with currently available clients

            For each request which needs to be started

            1. create (resource, client) pair with already running clients.
            2. create (resource, client) pair with available clients
            3. If all pairs are successfully made, starts service

            Note, must be called from within the protection of locked code.
        """
        for requests in self._requests.values():
            for request in requests:
                key = unique_id.toHexString(request.id)
                if key in self._pairs:
                    # nothing to touch
                    continue
                self.loginfo("processing outstanding request %s" % unique_id.toHexString(request.id))
                resources = copy.deepcopy(request.resources)
                app_pairs = []
                available_clients = self._get_available_clients()
                cname = [c.name for c in available_clients]
                self.loginfo("  available clients : " + str(cname))
                self.loginfo("  required resources : %s" % [resource.platform_info + "." + resource.name for resource in resources])

                # Check if any resource can be paired with already running client
                paired_resources, reused_client_app_pairs = self._pairs_with_running_clients(resources)

                # More client is required?
                remaining_resources = [resource for resource in resources if not resource in paired_resources]

                # More client is required?
                remaining_resources = [r for r in resources if not r in paired_resources]

                # Clients who are not running yet
                available_clients = self._get_available_clients()
                cname = [c.name for c in available_clients]
                self.loginfo("  remaining clients : " + str(cname))
                self.loginfo("  remaining resources : %s" % [resource.platform_info + "." + resource.name for resource in remaining_resources])

                # Pair between remaining node and free cleints
                status, message, new_app_pairs = demo_allocator.resolve(remaining_resources, available_clients)

                app_pairs.extend(reused_client_app_pairs)
                app_pairs.extend(new_app_pairs)

                # Starts request if it is ready
                if status is concert_msgs.ErrorCodes.SUCCESS:
                    request.status = scheduler_msgs.Request.GRANTED
                    self._pairs[key] = app_pairs
                    if self._start_request(new_app_pairs, key):
                        self._mark_clients_as_inuse(app_pairs)
                else:
                    # if not, do nothing
                    self.logwarn("warning in [" + key + "] status. " + str(message))

    def _pairs_with_running_clients(self, resources):
        """
            Check if clients already running can be reused for another resource.
            To be compatibile, it should have the same remapping rules, and available slot.

            @param resources: list of resources for a single request
            @type scheduler_msgs.Resource[]

            @return paired_resource: which can reuse inuse client
            @type scheduler_msgs.Resource[]

            @return new_app_pair: new pair
            @type [(concert_msgs.LinkNode, concert_msgs.ConcertClient)]
        """
        paired_resource = []
        new_app_pairs = []

        for r in resources:
            resource_app_name = r.name

            if resource_app_name in self._inuse:
                resource_remappings = r.remappings
                # key is client name, value is a tuple (client_node, remappings, max_share, count)
                for unused_client_name, tup in self._inuse[resource_app_name].items():
                    client_node, remappings, max_share, count = tup
                    if remappings == resource_remappings and count < max_share:
                        paired_resource.append(r)
                        new_app_pairs.append((r, client_node))

        return paired_resource, new_app_pairs

    def _mark_clients_as_inuse(self, pairs):
        """
            store the paired client in inuse_clients

            @param pair: list of pair (resource, client)
            @type : list of (scheduler_msgs.Resource, concert_msgs.ConcertClient)
        """
        #demo_allocator.print_pairs(pairs)

        for p in pairs:
            resource, client = p
            resource_app_name = resource.name
            remappings = resource.remappings

            if not resource_app_name in self._inuse:
                self._inuse[resource_app_name] = {}

            app = [a for a in client.apps if a.name == resource_app_name][0]  # must be only one

            if client.name in self._inuse[resource_app_name]:
                c, r, m, count = self._inuse[resource_app_name][client.name]
                self._inuse[resource_app_name][client.name] = (c, r, m, count + 1)
            else:
                self._inuse[resource_app_name][client.name] = (client, remappings, app.share, 1)

    def _unmark_client_as_inuse(self, gateway_name):
        """
            mark that client is not inuse.

            @param client_gateway_name
            @type string

            @return True if no service is using this client. so it stops the app
            @rtype bool
        """

        for app_name, client_node_dict in self._inuse.items():
            for client_node_name, tup in client_node_dict.items():
                client_node, remapping, max_share, count = tup

                if client_node.gateway_name == gateway_name:
                    if count > 1:
                        client_node_dict[client_node_name] = (client_node, remapping, max_share, count - 1)
                        return False
                    else:
                        client_node_dict[client_node_name] = (client_node, remapping, max_share, count - 1)
                        del self._inuse[app_name][client_node_name]
                        return True

    def _get_available_clients(self):
        """
            Search clients which are not in use yet.

            @return list of available clients
            @rtype concert_msgs.ConcertClient[]
        """
        clients = [self._clients[c] for c in self._clients if self._clients[c].client_status == concert_msgs.Constants.CONCERT_CLIENT_STATUS_CONNECTED]  # and self._clients[c].app_status == concert_msgs.Constants.APP_STATUS_STOPPED]
        inuse_gateways = self._get_inuse_gateways()
        free_clients = [c for c in clients if not (c.gateway_name in inuse_gateways)]

        return free_clients

    def _get_inuse_gateways(self):
        """
            Iterate nested inuse and make a list of gateways

            @return list of gateway_name
            @rtype string[]
        """
        inuse_gateways = []

        for client_node_dict in self._inuse.values():
            for client_node, unused_remapping, unused_max_share, count in client_node_dict.values():
                if count > 0:
                    inuse_gateways.append(client_node.gateway_name)

        return inuse_gateways

    def _start_request(self, pairs, request_id):
        """
            Starts up client apps for service

            @param pairs: list of pair for service
            @type [(concert_msgs.LinkNode, concert_msgs.ConcertClient)]

            @param service_name
            @type string

            @return true if all apps started their engines successfully
            @rtype Bool
        """
        if len(pairs) == 0:
            return

        self.loginfo("  starting apps for request '" + str(request_id) + "'")
        already_started_pairs = []
        try:
            for p in pairs:
                resource, client = p
                start_app_srv = self._get_start_client_app_service(client.gateway_name)
                req = self._create_startapp_request(resource)
                self.loginfo("    starting '%s' on client '%s'..." % (resource.name, client.name))
                resp = start_app_srv(req)
                if resp.started:
                    already_started_pairs.append(p)
                else:
                    message = resp.message
                    raise FailedToStartAppsException(message)
        except FailedToStartAppsException as e:
            self.logerr(" failed to start all apps [%s]" % str(e))
            for p in already_started_pairs:
                resource, client = p
                stop_app_srv = self._get_stop_client_app_service(client.gateway_name)
                req = self._create_stopapp_request(resource)
                stop_app_srv(req)
            return False
        return True

    def _get_start_client_app_service(self, gateway_name):
        """
            @param gateway_name: The client gateway name
            @type string

            @return srv: ROS service instance to start app
            @type rospy.ServiceProxy
        """
        srv_name = '/' + gateway_name + DemoScheduler.POSTFIX_START_APP
        rospy.wait_for_service(srv_name)

        srv = rospy.ServiceProxy(srv_name, rapp_manager_srvs.StartApp)

        return srv

    def _create_startapp_request(self, resource):
        """
            creates startapp request instance

            @param resource_app_name: name of app to start in client
            @type string

            @param resource_node_name
            @type string

            @param resource_name
            @type string
        """

        req = rapp_manager_srvs.StartAppRequest()
        req.name = resource.name
        req.remappings = resource.remappings

        return req

    def loginfo(self, msg):
        rospy.loginfo("Scheduler : " + str(msg))

    def logwarn(self, msg):
        rospy.logwarn("Scheduler : " + str(msg))

    def logerr(self, msg):
        rospy.logerr("Scheduler : " + str(msg))

    def spin(self):
        self.loginfo("In spin")
        rospy.spin()
