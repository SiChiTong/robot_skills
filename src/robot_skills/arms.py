#! /usr/bin/env python

import rospy
import tf_server
import visualization_msgs.msg
import std_msgs.msg

from actionlib import SimpleActionClient, GoalStatus
from control_msgs.msg import FollowJointTrajectoryGoal, FollowJointTrajectoryAction
from diagnostic_msgs.msg import DiagnosticArray
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from tue_manipulation.msg import GraspPrecomputeGoal, GraspPrecomputeAction
from tue_manipulation.msg import GripperCommandGoal, GripperCommandAction
from tue_msgs.msg import GripperCommand


class ArmState:
    """Specifies a State either OPEN or CLOSE"""
    OPEN = "open"
    CLOSE = "close"


class Arm(object):
    """
    A single arm can be either left or right, extends Arms:
    Use left or right to get arm while running from the python console

    Examples:
    >>> left.send_goal(0.265, 1, 0.816, 0, 0, 0, 60)
    or Equivalently:
    >>> left.send_goal(px=0.265, py=1, pz=0.816, yaw=0, pitch=0, roll=0, time_out=60, pre_grasp=False, frame_id='/amigo/base_link')

    #To open left gripper
    >>> left.send_gripper_goal_open(10)
    """
    def __init__(self, robot_name, side, tf_listener):
        self.robot_name = robot_name
        self.side = side
        if (self.side is "left") or (self.side is "right"):
            pass
        else:
            raise Exception("Side should be either: left or right")
        self.tf_listener = tf_listener

        self._occupied_by = None
        self._operational = True  # In simulation, there will be no hardware cb

        # Get stuff from the parameter server
        self.offset = self.load_param('skills/arm/offset/' + self.side)
        self.marker_to_grippoint_offset = self.load_param('skills/arm/offset/marker_to_grippoint')

        self.joint_names = self.load_param('skills/arm/joint_names')
        self.joint_names = [name + "_" + self.side for name in self.joint_names]

        self.default_configurations = self.load_param('skills/arm/default_configurations')
        self.default_trajectories   = self.load_param('skills/arm/default_trajectories')

        # listen to the hardware status to determine if the arm is available
        rospy.Subscriber("/amigo/hardware_status", DiagnosticArray, self.cb_hardware_status)

        # Init gripper actionlib
        self._ac_gripper = SimpleActionClient(
            "/" + robot_name + "/" + self.side + "_arm/gripper/action", GripperCommandAction)

        # Init graps precompute actionlib
        self._ac_grasp_precompute = SimpleActionClient(
            "/" + robot_name + "/" + self.side + "_arm/grasp_precompute", GraspPrecomputeAction)

        # Init joint trajectory action server
        self._ac_joint_traj = SimpleActionClient(
            "/" + robot_name + "/" + self.side + "_arm/joint_trajectory_action", FollowJointTrajectoryAction)

        # ToDo: don't hardcode?
        server_timeout = 0.25
        self._ac_gripper_present = self._ac_gripper.wait_for_server(timeout=rospy.Duration(server_timeout))
        if not self._ac_gripper_present:
            rospy.logwarn("Cannot find gripper {0} server".format(self.side))
        self._ac_grasp_precompute_present  = self._ac_grasp_precompute.wait_for_server(timeout=rospy.Duration(server_timeout))
        if not self._ac_grasp_precompute_present:
            rospy.logwarn("Cannot find grasp precompute {0} server".format(self.side))
        self._ac_joint_traj_present = self._ac_joint_traj.wait_for_server(timeout=rospy.Duration(server_timeout))
        if not self._ac_joint_traj_present:
            rospy.logwarn("Cannot find joint trajectory action server {0}".format(self.side))

        # Init marker publisher
        self._marker_publisher = rospy.Publisher(
            "/" + robot_name + "/" + self.side + "_arm/grasp_target",
            visualization_msgs.msg.Marker, queue_size=10
        )

    def load_param(self, param_name):
        '''
        Loads a parameter from the parameter server, namespaced by robot name
        '''
        return rospy.get_param('/' + self.robot_name + '/' + param_name)

    def close(self):
        try:
            rospy.loginfo("{0} arm cancelling all goals on all arm-related ACs on close".format(self.side))
        except AttributeError:
            print "Arm cancelling all goals on all arm-related ACs on close. Rospy is already deleted."

        self._ac_gripper.cancel_all_goals()
        self._ac_grasp_precompute.cancel_all_goals()
        self._ac_joint_traj.cancel_all_goals()

    @property
    def operational(self):
        '''
        The 'operational' property reflects the current hardware status of the arm.
        '''
        return self._operational

    def cb_hardware_status(self, msg):
        '''
        hardware_status callback to determine if the arms are operational
        '''
        diags = [diag for diag in msg.status if diag.name == self.side + '_arm']

        if len(diags) == 0:
            rospy.logwarn('no diagnostic msg received for the %s arm' % self.side)
        elif len(diags) != 1:
            rospy.logwarn('multiple diagnostic msgs received for the %s arm' % self.side)
        else:
            level = diags[0].level

            # 0. Stale
            # 1. Idle
            # 2. Operational
            # 3. Homing
            # 4. Error
            if level != 2:
                self._operational = False
            else:
                self._operational = True

    def send_goal(self, px, py, pz, roll, pitch, yaw,
                  timeout=30,
                  pre_grasp=False,
                  frame_id='/base_link',
                  first_joint_pos_only=False,
                  allowed_touch_objects=[]):
        """
        Send a arm to a goal:

        Using a position px, py, pz and orientation roll, pitch, yaw. A time
        out time_out. pre_grasp means go to an offset that is normally needed
        for things such as grasping. You can also specify the frame_id which
        defaults to base_link """

        # save the arguments for debugging later
        myargs = locals()

        # If necessary, prefix frame_id
        if frame_id.find(self.robot_name) < 0:
            frame_id = "/"+self.robot_name+frame_id
            rospy.loginfo("Grasp precompute frame id = {0}".format(frame_id))

        # Create goal:
        grasp_precompute_goal = GraspPrecomputeGoal()
        grasp_precompute_goal.goal.header.frame_id = frame_id
        grasp_precompute_goal.goal.header.stamp = rospy.Time.now()

        grasp_precompute_goal.PERFORM_PRE_GRASP = pre_grasp
        grasp_precompute_goal.FIRST_JOINT_POS_ONLY = first_joint_pos_only

        grasp_precompute_goal.allowed_touch_objects = allowed_touch_objects

        grasp_precompute_goal.goal.x = px
        grasp_precompute_goal.goal.y = py
        grasp_precompute_goal.goal.z = pz

        grasp_precompute_goal.goal.roll  = roll
        grasp_precompute_goal.goal.pitch = pitch
        grasp_precompute_goal.goal.yaw   = yaw

        self._publish_marker(grasp_precompute_goal, [1, 0, 0], "grasp_point")

        # Add tunable parameters

        grasp_precompute_goal.goal.x += self.offset['x']
        grasp_precompute_goal.goal.y += self.offset['y']
        grasp_precompute_goal.goal.z += self.offset['z']

        grasp_precompute_goal.goal.roll  += self.offset['roll']
        grasp_precompute_goal.goal.pitch +=  self.offset['pitch']
        grasp_precompute_goal.goal.yaw   += self.offset['yaw']

        # rospy.loginfo("Arm goal: {0}".format(grasp_precompute_goal))

        self._publish_marker(grasp_precompute_goal, [0, 1, 0], "grasp_point_corrected")

        # Send goal:

        if timeout == 0.0:
            self._ac_grasp_precompute.send_goal(grasp_precompute_goal)
            return True
        else:
            result = self._ac_grasp_precompute.send_goal_and_wait(
                grasp_precompute_goal,
                execute_timeout=rospy.Duration(timeout)
            )
            if result == GoalStatus.SUCCEEDED:
                return True
            else:
                # failure
                rospy.logerr('grasp precompute goal failed: \n%s', repr(myargs))
                return False

    def send_delta_goal(self, px, py, pz, roll, pitch, yaw, 
                            timeout=30, 
                            pre_grasp = False, 
                            frame_id = '/base_link', 
                            use_offset = False, 
                            first_joint_pos_only=False):
        """Send arm to an offset with respect to current position: 
        Using a position px,py,pz. An orientation roll,pitch,yaw. A time out time_out. 
        """

        # save the arguments for debugging later
        myargs = locals()
        
        # create goal:
        grasp_precompute_goal = GraspPrecomputeGoal()
        grasp_precompute_goal.delta.header.frame_id = frame_id
        grasp_precompute_goal.delta.header.stamp = rospy.Time.now()
        
        grasp_precompute_goal.PERFORM_PRE_GRASP = pre_grasp
        grasp_precompute_goal.FIRST_JOINT_POS_ONLY = first_joint_pos_only
        
        grasp_precompute_goal.delta.x = px
        grasp_precompute_goal.delta.y = py
        grasp_precompute_goal.delta.z = pz
        
        grasp_precompute_goal.delta.roll = roll
        grasp_precompute_goal.delta.pitch = pitch
        grasp_precompute_goal.delta.yaw = yaw
        
        #rospy.loginfo("Arm goal: {0}".format(grasp_precompute_goal))
        
        self._publish_marker(grasp_precompute_goal, "red")
        
        if timeout == 0.0:
            self._ac_grasp_precompute.send_goal(grasp_precompute_goal)
            return True
        else:
            result = self._ac_grasp_precompute.send_goal_and_wait(
                grasp_precompute_goal,
                execute_timeout=rospy.Duration(timeout)
            )
            if result == GoalStatus.SUCCEEDED:
                return True
            else:
                # failure
                rospy.logerr('grasp precompute goal failed: \n%s', repr(myargs))
                return False

    def send_joint_goal(self, configuration):
        '''
        Send a named joint goal (pose) defined in the parameter default_configurations to the arm
        '''
        if configuration in self.default_configurations:
            return self._send_joint_trajectory(
                [self.default_configurations[configuration]]
                )
        else:
            rospy.logwarn('Default configuration {0} does not exist'.format(configuration))
            return False

    def send_joint_trajectory(self, configuration):
        '''
        Send a named joint trajectory (sequence of poses) defined in the default_trajectories to
        the arm
        '''
        if configuration in self.default_trajectories:
            return self._send_joint_trajectory(self.default_trajectories[configuration])
        else:
            rospy.logwarn('Default trajectories {0} does not exist'.format(configuration))
            return False

    def reset(self):
        '''
        Put the arm into the 'reset' pose
        '''
        return self.send_joint_goal('reset')

    @property
    def occupied_by(self):
        '''
        The 'occupied_by' property will return the current entity that is in the gripper of this arm.
        '''
        return self._occupied_by

    @occupied_by.setter
    def occupied_by(self, value):
        self._occupied_by = value

    def send_gripper_goal(self, state, timeout=5.0):
        '''
        Send a GripperCommand to the gripper of this arm and wait for finishing
        '''
        goal = GripperCommandGoal()

        if state == 'open':
            goal.command.direction = GripperCommand.OPEN
        elif state == 'close':
            goal.command.direction = GripperCommand.CLOSE
        else:
            rospy.logerr('State shoulde be open or close, now it is {0}'.format(state))
            return False

        self._ac_gripper.send_goal(goal)
        if timeout == 0.0 or timeout is None:
            if state == 'open':
                self.occupied_by = None
            return True
        else:
            self._ac_gripper.wait_for_result(rospy.Duration(timeout))
            if self._ac_gripper.get_state() == GoalStatus.SUCCEEDED:
                if state == 'open':
                    self.occupied_by = None
                return True
            else:
                return False


    def handover_to_human(self, timeout=10):
        '''
        Handover an item from the gripper to a human.

        Feels if user slightly pulls or pushes the (item in the) arm. On timeout, it will return False.
        '''

        succeeded = False;

        pub = rospy.Publisher('/'+self.robot_name+'/handoverdetector_'+self.side+'/toggle_robot2human', std_msgs.msg.Bool, queue_size=1, latch = True)
        pub.publish(std_msgs.msg.Bool(True))

        try:
            rospy.wait_for_message('/'+self.robot_name+'/handoverdetector_'+self.side+'/result', std_msgs.msg.Bool, timeout)
            print '/'+self.robot_name+'/handoverdetector_'+self.side+'/result'
            return True
        except(rospy.ROSException), e:
            rospy.logerr(e)
            return False


    def handover_to_robot(self, timeout=10):
        '''
        Handover an item from a human to the robot.

        Feels if user slightly pushes an item in the gripper. On timeout, it will return False.
        '''

        succeeded = False;
        
        pub = rospy.Publisher('/'+self.robot_name+'/handoverdetector_'+self.side+'/toggle_human2robot', std_msgs.msg.Bool, queue_size=1, latch = True)
        pub.publish(std_msgs.msg.Bool(True))

        try:
            rospy.wait_for_message('/'+self.robot_name+'/handoverdetector_'+self.side+'/result', std_msgs.msg.Bool, timeout)
            print '/'+self.robot_name+'/handoverdetector_'+self.side+'/result'
            return True
        except(rospy.ROSException), e:
            rospy.logerr(e)
            return False


    def _send_joint_trajectory(self, joints_references, timeout=rospy.Duration(5)):
        '''
        Low level method that sends a array of joint references to the arm.

        If timeout is defined, it will wait for timeout*len(joints_reference) seconds for the
        completion of the actionlib goal. It will return True as soon as possible when the goal
        succeeded. On timeout, it will return False.
        '''
        # First: check if the actionlib is available
        if not self._ac_joint_traj_present:
            self._ac_joint_traj_present = self._ac_joint_traj.wait_for_server(timeout=rospy.Duration(0.25))
        # If still not available: return false
        if not self._ac_joint_traj_present:
            rospy.logwarn('Joint trajectory action is not present: joint goal not reached')
            return False

        time_from_start = rospy.Duration()
        ps = []
        for joints_reference in joints_references:
            if (len(joints_reference) != len(self.joint_names)):
                rospy.logwarn('Please use the correct %d number of joint references (current = %d'
                              % (len(self.joint_names), len(joints_references)))

            ps.append(JointTrajectoryPoint(
                positions=joints_reference,
                time_from_start=time_from_start))

        joint_trajectory = JointTrajectory(joint_names=self.joint_names,
                                           points=ps)
        goal = FollowJointTrajectoryGoal(trajectory=joint_trajectory, goal_time_tolerance=timeout)

        rospy.logdebug("Send {0} arm to jointcoords \n{1}".format(self.side, ps))
        self._ac_joint_traj.send_goal(goal)
        if timeout != 0.0:
            done = self._ac_joint_traj.wait_for_result(timeout*len(joints_references))
            if not done:
                rospy.logwarn("Cannot reach joint goal {0}".format(goal))
        return done

    def _publish_marker(self, goal, color, ns = ""):
        marker = visualization_msgs.msg.Marker()
        marker.header.frame_id = goal.goal.header.frame_id
        marker.header.stamp = rospy.Time.now()
        marker.type = 2
        marker.pose.position.x = goal.goal.x
        marker.pose.position.y = goal.goal.y
        marker.pose.position.z = goal.goal.z
        marker.lifetime = rospy.Duration(20.0)
        marker.scale.x = 0.05
        marker.scale.y = 0.05
        marker.scale.z = 0.05
        marker.ns = ns

        marker.color.a = 1
        marker.color.r = color[0]
        marker.color.g = color[1]
        marker.color.b = color[2]

        self._marker_publisher.publish(marker)


if __name__ == "__main__":
    rospy.init_node('amigo_arms_executioner', anonymous=True)
    tf_listener = tf_server.TFClient()

    left = Arm('amigo', "left", tf_listener)
    right = Arm('amigo', "right", tf_listener)
