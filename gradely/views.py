from rest_framework import generics, permissions, viewsets, serializers
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import action
from rest_framework_simplejwt.views import TokenObtainPairView

from datetime import datetime
from django.db import transaction
from django.shortcuts import get_object_or_404

from .models import User, Classroom, Quiz, Subject, Student, QuizResult
from .serializers import (
    UserProfileSerializer,
    FacultySignupSerializer, 
    UserApprovalSerializer, 
    ClassroomSerializer, 
    ClassroomDetailSerializer,
    QuizSerializer, 
    SubjectSerializer,
    StudentSerializer,
    StudentUploadSerializer,
    QuizDetailSerializer,
    QuizResultSerializer,
    CustomTokenObtainPairSerializer,
)

import pandas as pd

#====================================
#     AUTH WORKS
#====================================
class FacultySignupView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = FacultySignupSerializer
    permission_classes = [permissions.AllowAny]

# Admin: Lists faculty users that are not approved
class PendingFacultyListView(generics.ListAPIView):
    queryset = User.objects.filter(role=User.Role.FACULTY, is_approved=False)
    serializer_class = UserApprovalSerializer
    permission_classes = [permissions.IsAdminUser]

# Admin: approve or reject faculty signups
class ApproveFacultyView(generics.UpdateAPIView):
    queryset = User.objects.all()
    serializer_class = UserApprovalSerializer
    permission_classes = [permissions.IsAdminUser]
    lookup_field = 'pk'

    # OCustom patch method to ensure they only toggle approval
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

class RejectFacultyView(generics.DestroyAPIView):
    queryset = User.objects.all()
    permission_classes = [permissions.IsAdminUser]
    lookup_field = 'pk'

# To get the role on the passed token
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

# ====================================
#    USER PROFILE
# ====================================
class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint that allows users to be viewed.
    Using ReadOnlyModelViewSet so specific user details can be fetched
    but not deleted/created via this endpoint.
    """
    queryset = User.objects.all()
    serializer_class = UserProfileSerializer # Make sure to import this from serializers.py
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Optimization: You might want to restrict this so users can only see themselves
        # But for now, returning all is fine for internal logic
        return User.objects.all()

#====================================
#     FACULTY PAGES
#====================================
# Faculty: Dashboard page
class TeacherDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        classrooms = Classroom.objects.filter(teacher=user).select_related('subject')
        
        quizzes = Quiz.objects.filter(classroom__teacher=user).order_by('-created_at')[:5]

        total_students = set()
        for c in classrooms:
            for s in c.students.all():
                total_students.add(s.id)

        return Response({
            "classes": ClassroomSerializer(classrooms, many=True).data,
            "recent_quizzes": QuizSerializer(quizzes, many=True).data,
            "total_students": len(total_students),
            "total_classes": classrooms.count()
        })
    
# Faculty: Subjects page
class SubjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    """
    This handles EVERYTHING: 
    - GET /api/subjects/ (List)
    - POST /api/subjects/ (Create)
    - PUT /api/subjects/5/ (Update)
    - DELETE /api/subjects/5/ (Delete)
    """
    queryset = Subject.objects.all().order_by('code')
    serializer_class = SubjectSerializer
    permission_classes = [IsAuthenticated]

class ClassroomViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        # Use the Detailed serializer (with student list) for single items
        if self.action == 'retrieve':
            return ClassroomDetailSerializer
        # Use the standard serializer (lighter, no student list) for the list view
        return ClassroomSerializer

    def get_queryset(self):
        # Security: Only return classes where the logged-in user is the teacher
        return Classroom.objects.filter(teacher=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        # Automation: Automatically assign the logged-in user as the teacher
        serializer.save(teacher=self.request.user)

    @action(detail=True, methods=['post'])
    def copy_list(self, request, pk=None):
        """
        Copy all students from a source classroom to this classroom.
        Payload: { "source_classroom_id": 12 }
        """
        target_classroom = self.get_object()
        source_id = request.data.get('source_classroom_id')

        if not source_id:
            return Response({"error": "Source classroom ID required"}, status=400)

        # Get the source classroom (ensure the teacher owns it too)
        source_classroom = get_object_or_404(Classroom, id=source_id, teacher=request.user)

        # Bulk add students
        students_to_add = source_classroom.students.all()
        if not students_to_add.exists():
             return Response({"error": "Source classroom has no students."}, status=400)
             
        target_classroom.students.add(*students_to_add)

        return Response({
            "message": f"Successfully copied {students_to_add.count()} students from {source_classroom.section_name}."
        })

    @action(detail=True, methods=['post'])
    def remove_student(self, request, pk=None):
        """
        Removes a student from the classroom roster.
        Endpoint: POST /api/classrooms/{id}/remove_student/
        Payload: { "student_id": 12 }
        """
        classroom = self.get_object()
        student_id = request.data.get('student_id')

        if not student_id:
            return Response(
                {"error": "Student ID is required."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get the student object or return 404
        student = get_object_or_404(Student, id=student_id)

        # Check if the student is actually in this class
        if student in classroom.students.all():
            classroom.students.remove(student)
            return Response(
                {"message": f"Student {student.name} removed from class."}, 
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"error": "Student is not enrolled in this class."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

# Faculty: Uploading excel file of students
class UploadStudentsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, classroom_id, format=None):
        serializer = StudentUploadSerializer(data=request.data)
        
        if serializer.is_valid():
            uploaded_file = request.FILES['file']
            
            try:
                # Fetch the classroom we are adding students to
                classroom = Classroom.objects.get(id=classroom_id, teacher=request.user)

                df = pd.read_csv(uploaded_file)

                # ======================================================
                # AUTO-ID GENERATION LOGIC
                # ======================================================
                
                # Get the current Year Prefix (e.g., "26")
                year_prefix = datetime.now().strftime('%y')
                
                with transaction.atomic():
                    last_student = Student.objects.filter(
                        student_id__startswith=f"{year_prefix}-"
                    ).order_by('-student_id').first()

                    if last_student:
                        last_seq = int(last_student.student_id.split('-')[1])
                        current_seq = last_seq + 1
                    else:
                        current_seq = 1

                    students_to_create = []
                    
                    # Iterate rows and assign IDs locally
                    for index, row in df.iterrows():
                        name = str(row['name']).strip().title()
                        
                        # Format: "26" + "-" + "00001" (padded to 5 digits)
                        new_student_id = f"{year_prefix}-{current_seq:06d}"
                        
                        student_obj, created = Student.objects.get_or_create(
                            name=name,
                            defaults={'student_id': new_student_id}
                        )
                    
                        if created:
                            current_seq += 1

                        students_to_create.append(student_obj)

                    # Add everyone to the classroom
                    classroom.students.add(*students_to_create)
                
                return Response({
                    "status": "success", 
                    "message": "Students processed successfully"
                }, status=status.HTTP_200_OK)

            except Classroom.DoesNotExist:
                return Response({"error": "Classroom not found"}, status=status.HTTP_404_NOT_FOUND)
            except Exception as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class QuizViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = QuizSerializer

    def get_queryset(self):
        # Security: Only return quizzes belonging to classrooms where the user is the teacher
        return Quiz.objects.filter(classroom__teacher=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        # Security Check: Ensure the teacher owns the classroom they are attaching the quiz to
        classroom = serializer.validated_data['classroom']
        if classroom.teacher != self.request.user:
            raise serializers.ValidationError("You cannot create a quiz for a class you do not teach.")
        serializer.save()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return QuizDetailSerializer
        return QuizSerializer

    @action(detail=True, methods=['post'])
    def save_results(self, request, pk=None):
        quiz = self.get_object()
        results_data = request.data.get('results', [])
        
        saved_count = 0
        errors = []

        for item in results_data:
            sid_str = item.get('student_id')
            score = item.get('score')
            answers = item.get('student_answers', {}) # <--- GET ANSWERS

            if not sid_str:
                continue

            student = Student.objects.filter(student_id=sid_str).first()

            if not student:
                errors.append(f"Student ID '{sid_str}' not registered in the system.")
                continue

            # Save or Update
            QuizResult.objects.update_or_create(
                quiz=quiz,
                student=student,
                defaults={
                    'score_obtained': score,
                    'student_answers': answers # <--- SAVE TO MODEL
                }
            )
            saved_count += 1
        
        # Update Stats (Optional but recommended)
        quiz.update_statistics()

        return Response({
            "status": "success",
            "saved": saved_count,
            "errors": errors
        })