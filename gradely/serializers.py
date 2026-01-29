from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User, Subject, Classroom, Quiz, QuizResult, Student


# For faculty signup
class FacultySignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'first_name', 'last_name']

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            role=User.Role.FACULTY,
            is_approved=False
        )
        return user

# For admin approval
class UserApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'role', 'is_approved', 'date_joined']
        read_only_fields = ['email', 'role'] # Admin only changes is_approved

# For user details
class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'role', 'is_approved', 'date_joined']
        read_only_fields = ['email', 'role', 'is_approved', 'date_joined']

# For subjects
class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ['id', 'name', 'code', 'description', 'grade_level']

# For classrooms
class ClassroomSerializer(serializers.ModelSerializer):
    subject = SubjectSerializer(read_only=True)

    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), 
        source='subject',
        write_only=True
    )

    student_count = serializers.IntegerField(source='students.count', read_only=True)

    class Meta:
        model = Classroom
        fields = ['id', 'section_name', 'school_year', 'subject', 'subject_id', 'student_count', 'created_at']

class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = ['id', 'student_id', 'name']

# A detailed view that shows the students inside
class ClassroomDetailSerializer(ClassroomSerializer):
    students = StudentSerializer(many=True, read_only=True)

    class Meta(ClassroomSerializer.Meta):
        fields = ClassroomSerializer.Meta.fields + ['students']

    def get_students(self, obj):
        return [
            {
                "id": s.id, 
                "student_id": s.student_id, 
                "name": f"{s.name}"
            } 
            for s in obj.students.all().order_by('student_id')
        ]

# For quizzes
class QuizSerializer(serializers.ModelSerializer):
    classroom_name = serializers.CharField(source='classroom.section_name', read_only=True)
    subject_code = serializers.CharField(source='classroom.subject.code', read_only=True)

    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'total_score', 'created_at', 'classroom', 
            'classroom_name', 'subject_code',
            'mean_score', 'max_score', 'min_score', 'attendees_count'
        ]

# For uploading students
class StudentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    
    def validate_file(self, value):
        if not value.name.endswith(('.xlsx', '.xls', '.csv')):
            raise serializers.ValidationError("Please upload a valid Excel file (.xlsx, .xls, .csv)")
        return value

class QuizResultSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    student_id = serializers.CharField(source='student.student_id', read_only=True)

    class Meta:
        model = QuizResult
        fields = ['id', 'student_name', 'student_id', 'score_obtained', 'date_taken']

class QuizDetailSerializer(serializers.ModelSerializer):
    results = serializers.SerializerMethodField() 
    
    classroom_name = serializers.CharField(source='classroom.section_name', read_only=True)
    
    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'total_score', 'mean_score', 
            'attendees_count', 'classroom_name', 'results', 'created_at'
        ]
    
    def get_results(self, obj):
        all_students = obj.classroom.students.all().order_by('name') 
        
        existing_results = {res.student.id: res for res in obj.results.all()}
        
        data = []
        for student in all_students:
            result = existing_results.get(student.id)
            
            if result:
                # Case A: Student has taken the quiz
                data.append({
                    "id": result.id,
                    "student_name": student.name, 
                    "student_id": student.student_id,
                    "score_obtained": result.score_obtained,
                    "date_taken": result.date_taken # Using the field from your model
                })
            else:
                data.append({
                    "id": f"temp-{student.id}",
                    "student_name": student.name,
                    "student_id": student.student_id,
                    "score_obtained": None, 
                    "date_taken": None
                })
        
        return data

# For auth, adding the role and id of the user
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        # Get the standard token data (access/refresh)
        data = super().validate(attrs)

        # Add extra data to the response
        data['role'] = self.user.role
        data['id'] = self.user.id
        
        return data